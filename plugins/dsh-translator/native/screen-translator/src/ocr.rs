use crate::{emit, Segment};
use image::{imageops::FilterType, DynamicImage, GrayImage, RgbImage};
use ort::{session::Session, value::Tensor};
use std::{error::Error, io::{self, Read}, path::Path, time::Instant};
type Result<T> = std::result::Result<T, Box<dyn Error>>;

fn session(path: &Path) -> Result<Session> {
    Ok(Session::builder()?.with_intra_threads(2)?.with_inter_threads(1)?
        .with_parallel_execution(false)?
        .with_config_entry("session.intra_op.allow_spinning", "0")?
        .with_config_entry("session.inter_op.allow_spinning", "0")?
        .commit_from_file(path)?)
}

pub struct Engine { det: Session, rec: Session, keys: Vec<String> }
impl Engine {
    pub fn new(runtime: &Path) -> Result<Self> {
        let internal = runtime.join("_internal");
        ort::init_from(internal.join("onnxruntime/capi/onnxruntime.dll").to_string_lossy()).commit()?;
        let models = internal.join("rapidocr/models");
        let det = session(&models.join("PP-OCRv6_det_small.onnx"))?;
        let rec = session(&models.join("PP-OCRv6_rec_small.onnx"))?;
        let dictionary = rec.metadata()?.custom("character")?.ok_or("OCR model character dictionary missing")?;
        let mut keys = vec![String::new()];
        keys.extend(dictionary.lines().map(str::to_owned));
        keys.push(" ".into());
        Ok(Self{det,rec,keys})
    }
    pub fn recognize(&mut self, image: &RgbImage) -> Result<Vec<Segment>> {
        let (w,h) = image.dimensions();
        if w < 2 || h < 2 || u64::from(w)*u64::from(h)>32_000_000 {return Err("Invalid capture dimensions".into())}
        let scale = (1280.0 / w.max(h) as f32).min(1.5);
        let dw = ((w as f32*scale/32.0).round().max(1.0) as u32)*32;
        let dh = ((h as f32*scale/32.0).round().max(1.0) as u32)*32;
        let resized = image::imageops::resize(image,dw,dh,FilterType::Triangle);
        let pixels = (dw*dh) as usize;
        let mut input = vec![0f32; pixels*3];
        // RapidOCR's detector consumes BGR, normalized per channel.
        for (i,p) in resized.pixels().enumerate() {
            for c in 0..3 {input[c*pixels+i]=(p[2-c] as f32/255.0-[0.485,0.456,0.406][c])/[0.229,0.224,0.225][c];}
        }
        let output = self.det.run(ort::inputs![Tensor::from_array(([1,3,dh as usize,dw as usize],input))?])?;
        let (shape,prob) = output[0].try_extract_tensor::<f32>()?;
        let oh = shape[shape.len()-2] as u32; let ow=shape[shape.len()-1] as u32;
        let mask = GrayImage::from_raw(ow,oh,prob.iter().map(|v|if *v>0.3 {255}else{0}).collect()).ok_or("Invalid detection shape")?;
        let contours = imageproc::contours::find_contours::<i32>(&mask);
        let mut boxes=Vec::new();
        for contour in contours {
            if contour.border_type != imageproc::contours::BorderType::Outer || contour.points.len()<4 {continue}
            let l=contour.points.iter().map(|p|p.x).min().unwrap().max(0);
            let t=contour.points.iter().map(|p|p.y).min().unwrap().max(0);
            let r=contour.points.iter().map(|p|p.x).max().unwrap().min(ow as i32-1);
            let b=contour.points.iter().map(|p|p.y).max().unwrap().min(oh as i32-1);
            if r-l<3 || b-t<3 {continue}
            let mut score=0.0;let mut count=0;
            for y in t..=b {for x in l..=r {score+=prob[(y as u32*ow+x as u32) as usize];count+=1;}}
            if score/(count as f32)<0.45 {continue}
            let expand = ((r-l)*(b-t)) as f32*1.5/(2*(r-l+b-t)) as f32;
            let sx=w as f32/ow as f32;let sy=h as f32/oh as f32;
            let x=((l as f32-expand)*sx).floor().max(0.0) as u32;
            let y=((t as f32-expand)*sy).floor().max(0.0) as u32;
            let right=((r as f32+expand)*sx).ceil().min(w as f32) as u32;
            let bottom=((b as f32+expand)*sy).ceil().min(h as f32) as u32;
            if right>x && bottom>y { boxes.push((x,y,right-x,bottom-y)); }
        }
        drop(output);
        boxes.sort_by_key(|b|(b.1,b.0));
        if boxes.len()>300 {return Err("Too many text regions; select a smaller area".into())}
        let mut result=Vec::new();
        for (x,y,bw,bh) in boxes {
            let crop=image::imageops::crop_imm(image,x,y,bw,bh).to_image();
            let rw=(48.0*bw as f32/bh as f32).ceil().clamp(8.0,2048.0) as u32;
            let resized=image::imageops::resize(&crop,rw,48,FilterType::Triangle);
            let pixels=(rw*48) as usize;let mut data=vec![0f32;pixels*3];
            for (i,p) in resized.pixels().enumerate(){for c in 0..3 {data[c*pixels+i]=(p[2-c] as f32/255.0-0.5)/0.5;}}
            let output=self.rec.run(ort::inputs![Tensor::from_array(([1,3,48,rw as usize],data))?])?;
            let (shape,scores)=output[0].try_extract_tensor::<f32>()?;
            let classes=*shape.last().ok_or("Missing OCR output")? as usize;
            let (text,confidence)=decode(scores,classes,&self.keys);
            if !text.trim().is_empty() && confidence>=0.5 {
                result.push(Segment{id:format!("seg-{}",result.len()),text,left:x as i32,top:y as i32,width:bw as i32,height:bh as i32,confidence,translation:String::new()});
            }
        }
        Ok(result)
    }
}
fn decode(scores:&[f32],classes:usize,keys:&[String])->(String,f32){
    let mut text=String::new();let mut last=0;let mut confidence=0.0;let mut count=0;
    if classes==0{return (text,0.0)}
    for row in scores.chunks_exact(classes){
        let (index,score)=row.iter().enumerate().max_by(|a,b|a.1.total_cmp(b.1)).unwrap();
        if index!=0 && index!=last {if let Some(key)=keys.get(index){text.push_str(key);confidence+=*score;count+=1;}}
        last=index;
    }
    (text,if count>0{confidence/count as f32}else{0.0})
}
pub fn worker(args:&[String])->Result<()> {
    let runtime=Path::new(args.get(2).ok_or("Missing runtime path")?);
    let mut engine=Engine::new(runtime)?;
    emit(serde_json::json!({"type":"ready"}));
    // Framed raw RGB input stays in memory. EOF terminates and releases models.
    let mut input=io::stdin().lock();
    loop {
        let mut header=[0u8;8];
        match input.read_exact(&mut header){Ok(())=>{},Err(e) if e.kind()==io::ErrorKind::UnexpectedEof=>break,Err(e)=>return Err(e.into())}
        let w=u32::from_le_bytes(header[..4].try_into()?);let h=u32::from_le_bytes(header[4..].try_into()?);
        if w<2 || h<2 || u64::from(w)*u64::from(h)>32_000_000 {return Err("Invalid capture size".into())}
        let mut data=vec![0;(w*h*3) as usize];input.read_exact(&mut data)?;
        let image=RgbImage::from_raw(w,h,data).ok_or("Invalid image")?;let start=Instant::now();
        match engine.recognize(&image){Ok(segments)=>emit(serde_json::json!({"type":"recognized","segments":segments,"elapsedMs":start.elapsed().as_millis()})),Err(e)=>emit(serde_json::json!({"type":"error","error":e.to_string()}))}
    }
    Ok(())
}
pub fn benchmark(args:&[String])->Result<()> {
    let start=Instant::now();let mut engine=Engine::new(Path::new(args.get(2).ok_or("Missing runtime path")?))?;
    let load=start.elapsed().as_millis();let image:DynamicImage=image::open(args.get(3).ok_or("Missing fixture path")?)?;
    for run in 0..3 {let start=Instant::now();let segments=engine.recognize(&image.to_rgb8())?;emit(serde_json::json!({"type":"benchmark","run":run,"loadMs":load,"elapsedMs":start.elapsed().as_millis(),"segments":segments}));}
    Ok(())
}
#[cfg(test)] mod tests {
    use super::*;
    #[test] fn ctc_blank_separates_repeated_characters(){
        let keys=vec!["".into(),"a".into(),"b".into()];
        assert_eq!(decode(&[0.,1.,0.,0.,1.,0.,1.,0.,0.,0.,1.,0.],3,&keys).0,"aa");
    }
}
