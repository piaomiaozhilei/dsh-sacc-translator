use crate::{emit, Segment};
use image::RgbImage;
use serde::Deserialize;
use serde_json::json;
use std::{collections::VecDeque, error::Error, io::{BufRead,BufReader,Write}, mem::{size_of,zeroed}, ptr::{null,null_mut}, process::{Child,Command as Process,Stdio}, os::windows::process::CommandExt, sync::{mpsc,Mutex,OnceLock}, time::Duration};
use windows_sys::Win32::{Foundation::*, Graphics::Gdi::*, System::{LibraryLoader::*,Threading::*,Memory::*,DataExchange::*,JobObjects::*}, UI::{WindowsAndMessaging::*,Input::KeyboardAndMouse::*,HiDpi::*}};
use std::os::windows::io::AsRawHandle;
use windows_sys::Win32::UI::Controls::{DRAWITEMSTRUCT,ODS_DISABLED,ODS_SELECTED,ODS_FOCUS};

const WAKE:u32=WM_APP+1;
const HOTKEY:i32=0x5348;
const BAR:i32=56;
const ACCENT:u32=0x0068A619;
const SURFACE:u32=0x00272220;
const INK:u32=0x00F8FAF9;
#[derive(Deserialize,Default)]
pub struct Command {
    #[serde(rename="type")] kind:String,
    #[serde(default)] id:u64,
    #[serde(default)] segments:Vec<Segment>,
    #[serde(default)] error:String,
}
static COMMANDS:OnceLock<Mutex<VecDeque<Command>>>=OnceLock::new();
static mut APP:*mut App=null_mut();
struct Capture {width:i32,height:i32,left:i32,top:i32,bgra:Vec<u8>}
struct BackBuffer {dc:HDC,bitmap:HBITMAP,old:HGDIOBJ,width:i32,height:i32}
impl Drop for BackBuffer {fn drop(&mut self){unsafe{SelectObject(self.dc,self.old);DeleteObject(self.bitmap);DeleteDC(self.dc);}}}
#[derive(Clone,Copy)]
struct Drag {point:(i32,i32),rect:RECT,edges:u8}
struct App {
    hwnd:HWND, buttons:Vec<HWND>, capture:Option<Capture>, blurred:Option<Capture>, backdrop:Option<Capture>, text_color:u32, buffer:Option<BackBuffer>, selection:Option<RECT>, drag:Option<Drag>, toolbar:RECT,
    selecting:bool, busy:bool, original:bool, segments:Vec<Segment>, status:String,id:u64,
    ocr:mpsc::SyncSender<(u64,RgbImage)>,
}
fn wide(s:&str)->Vec<u16>{s.encode_utf16().chain(Some(0)).collect()}
fn push(hwnd:isize,command:Command){
    COMMANDS.get().unwrap().lock().unwrap().push_back(command);
    unsafe {PostMessageW(hwnd as HWND,WAKE,0,0);}
}
fn ocr_thread(hwnd:isize,runtime:String,rx:mpsc::Receiver<(u64,RgbImage)>){
    // OS-owned lifetime: helper termination cannot leave inference children alive.
    let job=unsafe{CreateJobObjectW(null(),null())};
    if job.is_null(){emit(json!({"type":"error","error":"无法创建 OCR 进程管理器"}));return}
    unsafe {let mut info:JOBOBJECT_EXTENDED_LIMIT_INFORMATION=zeroed();info.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        if SetInformationJobObject(job,JobObjectExtendedLimitInformation,(&info as *const JOBOBJECT_EXTENDED_LIMIT_INFORMATION).cast(),size_of::<JOBOBJECT_EXTENDED_LIMIT_INFORMATION>() as u32)==0{CloseHandle(job);return}}
    let mut process:Option<(Child,BufReader<std::process::ChildStdout>)>=None;
    loop {
        match rx.recv_timeout(Duration::from_secs(45)) {
            Ok((id,image))=>{
                let result=(||->Result<Vec<Segment>,Box<dyn Error>>{
                    if process.is_none(){
                        let mut child=Process::new(std::env::current_exe()?).arg("--ocr").arg(&runtime)
                            .creation_flags(CREATE_NO_WINDOW).stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn()?;
                        if unsafe{AssignProcessToJobObject(job,child.as_raw_handle())}==0{let _=child.kill();let _=child.wait();return Err("无法管理 OCR 子进程".into())}
                        let mut out=BufReader::new(child.stdout.take().unwrap());let mut line=String::new();
                        out.read_line(&mut line)?;
                        let ready:serde_json::Value=serde_json::from_str(&line)?;
                        if ready["type"]!="ready"{let _=child.kill();let _=child.wait();return Err(ready["error"].as_str().unwrap_or("OCR start failed").into())}
                        process=Some((child,out));
                    }
                    let (child,out)=process.as_mut().unwrap();let input=child.stdin.as_mut().unwrap();
                    input.write_all(&image.width().to_le_bytes())?;input.write_all(&image.height().to_le_bytes())?;
                    input.write_all(image.as_raw())?;input.flush()?;
                    let mut line=String::new();out.read_line(&mut line)?;let response:serde_json::Value=serde_json::from_str(&line)?;
                    if response["type"]!="recognized"{return Err(response["error"].as_str().unwrap_or("OCR failed").into())}
                    emit(json!({"type":"metrics","ocrMs":response["elapsedMs"]}));
                    Ok(serde_json::from_value(response["segments"].clone())?)
                })();
                match result {
                    Ok(segments)=>push(hwnd,Command{kind:"ocr".into(),id,segments,..Default::default()}),
                    Err(e)=>{if let Some((mut child,_))=process.take(){let _=child.kill();let _=child.wait();}
                        push(hwnd,Command{kind:"error".into(),id,error:e.to_string(),..Default::default()});}
                }
            },
            Err(mpsc::RecvTimeoutError::Timeout)=>{if let Some((mut child,_))=process.take(){let _=child.kill();let _=child.wait();emit(json!({"type":"ocrReleased"}));}},
            Err(_)=>break,
        }
    }
    if let Some((mut child,_))=process {let _=child.kill();let _=child.wait();}
    unsafe{CloseHandle(job);}
}
unsafe fn capture()->Result<Capture,Box<dyn Error>> {
    let left=GetSystemMetrics(SM_XVIRTUALSCREEN);let top=GetSystemMetrics(SM_YVIRTUALSCREEN);
    let width=GetSystemMetrics(SM_CXVIRTUALSCREEN);let height=GetSystemMetrics(SM_CYVIRTUALSCREEN);
    if width<=0 || height<=0 || i64::from(width)*i64::from(height)>64_000_000 {return Err("屏幕尺寸过大，请降低显示分辨率".into())}
    let screen=GetDC(null_mut());let memory=CreateCompatibleDC(screen);
    let info=bitmap_info(width,height);let mut bits=null_mut();
    let bitmap=CreateDIBSection(screen,&info,DIB_RGB_COLORS,&mut bits,null_mut(),0);
    if bitmap.is_null() {DeleteDC(memory);ReleaseDC(null_mut(),screen);return Err("截图缓冲区创建失败".into())}
    let old=SelectObject(memory,bitmap);let ok=BitBlt(memory,0,0,width,height,screen,left,top,SRCCOPY|CAPTUREBLT);
    let bgra=if ok!=0{std::slice::from_raw_parts(bits as *const u8,(width*height*4) as usize).to_vec()}else{Vec::new()};
    SelectObject(memory,old);DeleteObject(bitmap);DeleteDC(memory);ReleaseDC(null_mut(),screen);
    if ok==0{return Err("无法截取屏幕".into())}
    Ok(Capture{width,height,left,top,bgra})
}
fn bitmap_info(width:i32,height:i32)->BITMAPINFO {
    let mut info:BITMAPINFO=unsafe{zeroed()};info.bmiHeader.biSize=size_of::<BITMAPINFOHEADER>() as u32;
    info.bmiHeader.biWidth=width;info.bmiHeader.biHeight=-height;info.bmiHeader.biPlanes=1;info.bmiHeader.biBitCount=32;info
}
fn normalize(a:(i32,i32),b:(i32,i32),width:i32,height:i32)->RECT {
    RECT{left:a.0.min(b.0).clamp(0,width),top:a.1.min(b.1).clamp(0,height),right:a.0.max(b.0).clamp(0,width),bottom:a.1.max(b.1).clamp(0,height)}
}
unsafe fn close(app:&mut App){
    ReleaseCapture();ShowWindow(app.hwnd,SW_HIDE);app.id+=1;app.busy=false;app.selecting=false;app.capture=None;app.blurred=None;app.backdrop=None;app.buffer=None;app.selection=None;app.drag=None;app.segments.clear();
    emit(json!({"type":"closed"}));
}
fn frosted_region(image:&RgbImage)->(Capture,u32){
    // Match SACC's continuous backdrop: dominant color + 74% tint over Gaussian blur.
    let small=image::imageops::resize(image,(image.width()/4).max(1),(image.height()/4).max(1),image::imageops::FilterType::Triangle);
    let mut counts=std::collections::HashMap::<[u8;3],usize>::new();
    for p in small.pixels(){*counts.entry([p[0]/16*16,p[1]/16*16,p[2]/16*16]).or_default()+=1;}
    let bg=counts.into_iter().max_by_key(|(color,count)|(*count,*color)).map(|(c,_)|c.map(|v|v.saturating_add(8))).unwrap_or([24,32,40]);
    let radius=((image.width()+image.height()) as f32/160.0).clamp(4.0,14.0)/4.0;
    let blur=image::imageops::blur(&small,radius);let mut bgra=Vec::with_capacity((blur.width()*blur.height()*4) as usize);
    for p in blur.pixels(){let c=std::array::from_fn::<_,3,_>(|i|((p[i] as u16*26+bg[i] as u16*74)/100) as u8);bgra.extend_from_slice(&[c[2],c[1],c[0],255]);}
    let bright=(bg[0] as u32*299+bg[1] as u32*587+bg[2] as u32*114)/1000>150;
    (Capture{width:blur.width() as i32,height:blur.height() as i32,left:0,top:0,bgra},if bright{0x0020120B}else{INK})
}
fn blur_background(frame:&Capture)->Capture{
    // Gaussian blur at quarter resolution is computed once per invocation.
    // Keep only this small cached bitmap; never blur during pointer movement.
    let source=image::RgbaImage::from_raw(frame.width as u32,frame.height as u32,frame.bgra.clone()).unwrap();
    let small=image::imageops::resize(&source,(frame.width as u32/4).max(1),(frame.height as u32/4).max(1),image::imageops::FilterType::Triangle);
    let mut blurred=image::imageops::blur(&small,2.2);
    for pixel in blurred.pixels_mut(){for channel in &mut pixel.0[..3]{*channel=((*channel as u16*32)/100+8) as u8;}pixel.0[3]=255;}
    Capture{width:blurred.width() as i32,height:blurred.height() as i32,left:frame.left,top:frame.top,bgra:blurred.into_raw()}
}
unsafe fn begin_selection(app:&mut App,frame:Capture){
    app.id+=1;app.status="拖动框选 · Enter 翻译 · Esc 退出".into();app.selecting=true;app.busy=false;app.drag=None;app.selection=None;app.segments.clear();
    app.blurred=Some(blur_background(&frame));app.capture=Some(frame);app.backdrop=None;app.buffer=None;
    let frame=app.capture.as_ref().unwrap();
    SetWindowPos(app.hwnd,HWND_TOPMOST,frame.left,frame.top,frame.width,frame.height,SWP_SHOWWINDOW);
    SetForegroundWindow(app.hwnd);SetFocus(app.hwnd);InvalidateRect(app.hwnd,null(),0);
}
unsafe fn select(app:&mut App){
    if app.busy||app.selecting {return}
    ShowWindow(app.hwnd,SW_HIDE);
    for button in &app.buttons {ShowWindow(*button,SW_HIDE);}
    match capture(){
        Ok(frame)=>begin_selection(app,frame),
        Err(e)=>emit(json!({"type":"error","error":e.to_string()})),
    }
}
unsafe fn finish_selection(app:&mut App){
    if !app.selecting{return}
    let Some(rect)=app.selection else{return};
    let width=rect.right-rect.left;let height=rect.bottom-rect.top;
    if width<12||height<12 {return}
    if i64::from(width)*i64::from(height)>32_000_000 {app.status="选区过大，请缩小选区".into();InvalidateRect(app.hwnd,null(),0);return}
    let Some(frame)=app.capture.as_ref() else{return};let mut rgb=Vec::with_capacity((width*height*3) as usize);
    for y in 0..height {
        let start=(((y+rect.top)*frame.width+rect.left)*4) as usize;
        let row=&frame.bgra[start..start+(width*4) as usize];
        for p in row.chunks_exact(4){rgb.extend_from_slice(&[p[2],p[1],p[0]]);}
    }
    let image=RgbImage::from_raw(width as u32,height as u32,rgb).unwrap();
    let (backdrop,color)=frosted_region(&image);app.backdrop=Some(backdrop);app.text_color=color;
    app.busy=true;app.selecting=false;app.original=false;app.drag=None;app.status="正在识别选区文字…".into();layout_toolbar(app);
    InvalidateRect(app.hwnd,null(),0);emit(json!({"type":"busy","id":app.id}));
    if app.ocr.try_send((app.id,image)).is_err(){app.busy=false;app.selecting=true;app.status="OCR 正忙，请稍后点击翻译选区".into();layout_toolbar(app);emit(json!({"type":"error","error":app.status}));}
}
fn hit_edges(rect:RECT,p:(i32,i32))->u8{
    if p.0<rect.left-8||p.0>rect.right+8||p.1<rect.top-8||p.1>rect.bottom+8{return 0}
    let mut edges=0;
    if (p.0-rect.left).abs()<=8{edges|=1;}else if (p.0-rect.right).abs()<=8{edges|=2;}
    if (p.1-rect.top).abs()<=8{edges|=4;}else if (p.1-rect.bottom).abs()<=8{edges|=8;}
    if edges==0{16}else{edges}
}
fn dragged(drag:Drag,p:(i32,i32),width:i32,height:i32)->RECT{
    let mut r=drag.rect;let dx=p.0-drag.point.0;let dy=p.1-drag.point.1;
    if drag.edges==0{return normalize(drag.point,p,width,height)}
    if drag.edges==16{
        let dx=dx.clamp(-r.left,width-r.right);let dy=dy.clamp(-r.top,height-r.bottom);
        r.left+=dx;r.right+=dx;r.top+=dy;r.bottom+=dy;return r
    }
    if drag.edges&1!=0{r.left=(r.left+dx).clamp(0,r.right-12);}
    if drag.edges&2!=0{r.right=(r.right+dx).clamp(r.left+12,width);}
    if drag.edges&4!=0{r.top=(r.top+dy).clamp(0,r.bottom-12);}
    if drag.edges&8!=0{r.bottom=(r.bottom+dy).clamp(r.top+12,height);}
    r
}
unsafe fn layout_toolbar(app:&mut App){
    let Some(r)=app.selection else{for button in &app.buttons{ShowWindow(*button,SW_HIDE);}return};let Some(frame)=&app.capture else{return};
    let width=500.min(frame.width);let left=(r.right-width).clamp(0,frame.width-width);
    let top=if r.bottom+BAR+12<=frame.height{r.bottom+12}else if r.top>=BAR+12{r.top-BAR-12}else{(frame.height-BAR).max(0)};
    app.toolbar=RECT{left,top,right:left+width,bottom:top+BAR};
    for (i,button) in app.buttons.iter().enumerate(){
        SetWindowPos(*button,null_mut(),left+8+i as i32*(width-16)/5,top+8,(width-16)/5-4,BAR-16,SWP_NOZORDER|SWP_SHOWWINDOW);
        EnableWindow(*button,if i==0{!app.busy}else if i==2{!app.busy&&!app.segments.is_empty()}else if i==3{!app.segments.is_empty()}else{true} as i32);
    }
    SetWindowTextW(app.buttons[0],wide(if app.selecting{"翻译选区"}else if app.original{"查看译文"}else{"查看原文"}).as_ptr());
}
unsafe fn text(dc:HDC,content:&str,rect:&mut RECT,size:i32,color:u32,flags:u32){
    let font=CreateFontW(-size,0,0,0,400,0,0,0,DEFAULT_CHARSET as u32,0,0,CLEARTYPE_QUALITY as u32,0,wide("Microsoft YaHei UI").as_ptr());
    let old=SelectObject(dc,font);SetTextColor(dc,color);SetBkMode(dc,TRANSPARENT as i32);
    let mut value=wide(content);DrawTextW(dc,value.as_mut_ptr(),(value.len()-1) as i32,rect,flags);
    SelectObject(dc,old);DeleteObject(font);
}
unsafe fn fill(dc:HDC,rect:&RECT,color:u32){let brush=CreateSolidBrush(color);FillRect(dc,rect,brush);DeleteObject(brush);}
unsafe fn rounded(dc:HDC,r:&RECT,color:u32,border:u32,radius:i32){
    let brush=CreateSolidBrush(color);let pen=CreatePen(PS_SOLID,1,border);let old_b=SelectObject(dc,brush);let old_p=SelectObject(dc,pen);
    RoundRect(dc,r.left,r.top,r.right,r.bottom,radius,radius);SelectObject(dc,old_b);SelectObject(dc,old_p);DeleteObject(brush);DeleteObject(pen);
}
unsafe fn outline(dc:HDC,r:RECT,editable:bool){
    let pen=CreatePen(PS_SOLID,2,ACCENT);let old=SelectObject(dc,pen);let brush=SelectObject(dc,GetStockObject(HOLLOW_BRUSH));
    Rectangle(dc,r.left,r.top,r.right,r.bottom);SelectObject(dc,brush);SelectObject(dc,old);DeleteObject(pen);
    if editable {for (x,y) in [(r.left,r.top),((r.left+r.right)/2,r.top),(r.right,r.top),(r.left,(r.top+r.bottom)/2),(r.right,(r.top+r.bottom)/2),(r.left,r.bottom),((r.left+r.right)/2,r.bottom),(r.right,r.bottom)]{
        rounded(dc,&RECT{left:x-4,top:y-4,right:x+5,bottom:y+5},INK,ACCENT,3);
    }}
}
unsafe fn paint(app:&mut App){
    let mut ps:PAINTSTRUCT=zeroed();let screen=BeginPaint(app.hwnd,&mut ps);let mut client:RECT=zeroed();GetClientRect(app.hwnd,&mut client);
    if app.buffer.as_ref().is_none_or(|b|b.width!=client.right||b.height!=client.bottom){
        app.buffer=None;let dc=CreateCompatibleDC(screen);let bitmap=CreateCompatibleBitmap(screen,client.right.max(1),client.bottom.max(1));
        if !dc.is_null()&&!bitmap.is_null(){let old=SelectObject(dc,bitmap);app.buffer=Some(BackBuffer{dc,bitmap,old,width:client.right,height:client.bottom});}
        else{if !dc.is_null(){DeleteDC(dc);}if !bitmap.is_null(){DeleteObject(bitmap);}}
    }
    let dc=app.buffer.as_ref().map(|b|b.dc).unwrap_or(screen);
    fill(dc,&client,SURFACE);
    if let Some(frame)=&app.capture {
        if let Some(blur)=&app.blurred{
            SetStretchBltMode(dc,HALFTONE as i32);
            StretchDIBits(dc,0,0,frame.width,frame.height,0,0,blur.width,blur.height,blur.bgra.as_ptr().cast(),&bitmap_info(blur.width,blur.height),DIB_RGB_COLORS,SRCCOPY);
        }
        if let Some(r)=app.selection{
            let saved=SaveDC(dc);IntersectClipRect(dc,r.left,r.top,r.right,r.bottom);
            if app.selecting||app.original{
                StretchDIBits(dc,0,0,frame.width,frame.height,0,0,frame.width,frame.height,frame.bgra.as_ptr().cast(),&bitmap_info(frame.width,frame.height),DIB_RGB_COLORS,SRCCOPY);
            }else if let Some(backdrop)=&app.backdrop{
                StretchDIBits(dc,r.left,r.top,r.right-r.left,r.bottom-r.top,0,0,backdrop.width,backdrop.height,backdrop.bgra.as_ptr().cast(),&bitmap_info(backdrop.width,backdrop.height),DIB_RGB_COLORS,SRCCOPY);
            }
            if !app.selecting&&!app.original {for segment in &app.segments{
                if segment.translation.is_empty(){continue}
                let mut rect=RECT{left:r.left+segment.left.max(0),top:r.top+segment.top,right:(r.left+segment.left+segment.width).min(r.right),bottom:(r.top+segment.top+segment.height).min(r.bottom)};
                let mut size=(segment.height-2).clamp(11,24);
                while size>9 {let mut measure=rect;text(dc,&segment.translation,&mut measure,size,app.text_color,DT_WORDBREAK|DT_CALCRECT|DT_NOPREFIX);if measure.bottom-measure.top<=rect.bottom-rect.top{break}size-=1;}
                text(dc,&segment.translation,&mut rect,size,app.text_color,DT_WORDBREAK|DT_NOPREFIX);
            }}
            RestoreDC(dc,saved);outline(dc,r,app.selecting);
            if app.drag.is_none(){rounded(dc,&app.toolbar,SURFACE,0x00514A46,16);}
            let label=if app.selecting{format!("{} × {}  ·  拖动边缘调整",r.right-r.left,r.bottom-r.top)}else if app.busy{app.status.clone()}else if !app.status.is_empty(){app.status.clone()}else{"翻译完成 · Esc 退出".into()};
            let label_width=360.min(frame.width);let x=r.left.clamp(0,frame.width-label_width);
            let y=if r.top>=40{r.top-36}else{(r.bottom+BAR+28).min((frame.height-36).max(0))};
            let mut badge=RECT{left:x,top:y,right:x+label_width,bottom:y+28};rounded(dc,&badge,SURFACE,0x00514A46,10);
            badge.left+=12;badge.right-=12;text(dc,&label,&mut badge,14,INK,DT_LEFT|DT_VCENTER|DT_SINGLELINE|DT_END_ELLIPSIS|DT_NOPREFIX);
        }else{
            let width=410.min(frame.width);let mut hint=RECT{left:(frame.width-width)/2,top:24,right:(frame.width+width)/2,bottom:76};
            rounded(dc,&hint,SURFACE,0x00514A46,18);text(dc,&app.status,&mut hint,16,INK,DT_CENTER|DT_VCENTER|DT_SINGLELINE);
        }
    }
    // Only publish the completed frame: no visible erase/bitmap/border phases.
    if dc!=screen{BitBlt(screen,ps.rcPaint.left,ps.rcPaint.top,ps.rcPaint.right-ps.rcPaint.left,ps.rcPaint.bottom-ps.rcPaint.top,dc,ps.rcPaint.left,ps.rcPaint.top,SRCCOPY);}
    EndPaint(app.hwnd,&ps);
}
unsafe fn draw_button(draw:&DRAWITEMSTRUCT,app:&App){
    let index=(draw.CtlID-1001) as usize;if index>=app.buttons.len(){return}
    fill(draw.hDC,&draw.rcItem,SURFACE);
    let disabled=draw.itemState&ODS_DISABLED!=0;let pressed=draw.itemState&ODS_SELECTED!=0;let focused=draw.itemState&ODS_FOCUS!=0;
    let primary=index==0&&app.selecting&&!disabled;
    let color=if pressed{0x004D443B}else if primary{ACCENT}else{SURFACE};
    let mut rect=draw.rcItem;rect.left+=1;rect.top+=1;rect.right-=1;rect.bottom-=1;
    rounded(draw.hDC,&rect,color,if focused{INK}else{color},10);
    let mut label=[0u16;64];let len=GetWindowTextW(draw.hwndItem,label.as_mut_ptr(),64);
    text(draw.hDC,&String::from_utf16_lossy(&label[..len as usize]),&mut rect,14,if disabled{0x00837D77}else{INK},DT_CENTER|DT_VCENTER|DT_SINGLELINE);
}
unsafe fn copy(app:&App){
    let value=app.segments.iter().map(|s|if s.translation.is_empty(){s.text.as_str()}else{s.translation.as_str()}).collect::<Vec<_>>().join("\n");
    if value.is_empty()||OpenClipboard(app.hwnd)==0{return}
    let value=wide(&value);let memory=GlobalAlloc(GMEM_MOVEABLE,value.len()*2);
    if !memory.is_null(){let ptr=GlobalLock(memory);if !ptr.is_null(){std::ptr::copy_nonoverlapping(value.as_ptr(),ptr.cast(),value.len());GlobalUnlock(memory);EmptyClipboard();if SetClipboardData(13,memory).is_null(){GlobalFree(memory);}}else{GlobalFree(memory);}}
    CloseClipboard();
}
unsafe fn action(app:&mut App,index:usize){
    match index{
        0 if app.selecting=>finish_selection(app),
        0 if !app.busy=>{app.original=!app.original;layout_toolbar(app);InvalidateRect(app.hwnd,null(),0);},
        1=>{app.id+=1;app.busy=false;app.selecting=true;app.original=false;app.selection=None;app.drag=None;app.segments.clear();app.status="拖动框选 · Enter 翻译 · Esc 退出".into();for button in &app.buttons{ShowWindow(*button,SW_HIDE);}emit(json!({"type":"closed"}));SetFocus(app.hwnd);InvalidateRect(app.hwnd,null(),0);},
        2 if !app.busy&&!app.segments.is_empty()=>{app.busy=true;app.status="正在重新翻译…".into();layout_toolbar(app);emit(json!({"type":"recognized","id":app.id,"segments":app.segments}));InvalidateRect(app.hwnd,null(),0);},
        3=>{copy(app);app.status="已复制译文".into();InvalidateRect(app.hwnd,null(),0);},4=>close(app),_=>{}
    }
}
unsafe extern "system" fn wndproc(hwnd:HWND,msg:u32,w:WPARAM,l:LPARAM)->LRESULT {
    if APP.is_null(){return DefWindowProcW(hwnd,msg,w,l)}
    let app=&mut *APP;
    match msg {
        WM_HOTKEY=>{select(app);0},
        WM_PAINT=>{paint(app);0},
        WM_ERASEBKGND=>1,
        WM_DRAWITEM=>{draw_button(&*(l as *const DRAWITEMSTRUCT),app);1},
        WM_KEYDOWN if w==VK_ESCAPE as usize=>{close(app);0},
        WM_CLOSE=>{close(app);0},
        WM_COMMAND=>{let id=w&0xffff;if (1001..=1005).contains(&id){action(app,id-1001);}else if id==2 {close(app);}0},
        WM_LBUTTONDOWN=>{
            let point=((l as u32&0xffff) as i16 as i32,((l as u32>>16)&0xffff) as i16 as i32);
            if app.selecting{
                let rect=app.selection.unwrap_or(RECT{left:point.0,top:point.1,right:point.0,bottom:point.1});
                let edges=app.selection.map(|r|hit_edges(r,point)).unwrap_or(0);
                app.drag=Some(Drag{point,rect,edges});for button in &app.buttons{ShowWindow(*button,SW_HIDE);}SetFocus(hwnd);SetCapture(hwnd);
            }
            0
        },
        WM_MOUSEMOVE if app.selecting=>{
            let point=((l as u32&0xffff) as i16 as i32,((l as u32>>16)&0xffff) as i16 as i32);
            let edges=app.drag.map(|d|d.edges).unwrap_or_else(||app.selection.map(|r|hit_edges(r,point)).unwrap_or(0));
            SetCursor(LoadCursorW(null_mut(),match edges{1|2=>IDC_SIZEWE,4|8=>IDC_SIZENS,5|10=>IDC_SIZENWSE,6|9=>IDC_SIZENESW,16=>IDC_SIZEALL,_=>IDC_CROSS}));
            if let (Some(frame),Some(drag))=(&app.capture,app.drag){app.selection=Some(dragged(drag,point,frame.width,frame.height));InvalidateRect(hwnd,null(),0);}0
        },
        WM_LBUTTONUP if app.selecting=>{
            if let (Some(frame),Some(drag))=(&app.capture,app.drag){let point=((l as u32&0xffff) as i16 as i32,((l as u32>>16)&0xffff) as i16 as i32);app.selection=Some(dragged(drag,point,frame.width,frame.height));}
            app.drag=None;ReleaseCapture();
            if app.selection.is_some_and(|r|r.right-r.left<12||r.bottom-r.top<12){app.selection=None;}
            layout_toolbar(app);InvalidateRect(hwnd,null(),0);0
        },
        WM_CAPTURECHANGED if app.selecting=>{app.drag=None;layout_toolbar(app);InvalidateRect(hwnd,null(),0);0},
        WAKE=>{
            let commands:Vec<_>=COMMANDS.get().unwrap().lock().unwrap().drain(..).collect();
            for command in commands {match command.kind.as_str(){
                "select"=>select(app),"close"=>close(app),"shutdown"=>{UnregisterHotKey(hwnd,HOTKEY);DestroyWindow(hwnd);PostQuitMessage(0);},
                "ocr" if command.id==app.id=>{
                    app.segments=command.segments;if app.segments.is_empty(){app.busy=false;app.status="未识别到文字，请重新框选".into();emit(json!({"type":"error","error":app.status}));}
                    else{app.status="正在翻译…".into();emit(json!({"type":"recognized","id":app.id,"segments":app.segments}));}
                    layout_toolbar(app);InvalidateRect(hwnd,null(),0);
                },
                "translated" if command.id==app.id=>{app.segments=command.segments;app.busy=false;app.status=String::new();layout_toolbar(app);InvalidateRect(hwnd,null(),0);emit(json!({"type":"completed","id":app.id}));},
                "error" if command.id==app.id=>{app.busy=false;app.status=command.error;layout_toolbar(app);InvalidateRect(hwnd,null(),0);emit(json!({"type":"error","error":app.status}));},_=>{}
            }}0
        },
        _=>DefWindowProcW(hwnd,msg,w,l),
    }
}
pub fn run()->Result<(),Box<dyn Error>> {
    let runtime=std::env::args().nth(1).ok_or("Missing OCR runtime path")?;
    COMMANDS.set(Mutex::new(VecDeque::new())).map_err(|_|"Already initialized")?;
    unsafe {
        SetProcessDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2);
        let instance=GetModuleHandleW(null());let name=wide("SaccRustScreenTranslator");
        let mut wc:WNDCLASSW=zeroed();wc.hInstance=instance;wc.lpfnWndProc=Some(wndproc);wc.lpszClassName=name.as_ptr();wc.hCursor=LoadCursorW(null_mut(),IDC_CROSS);
        if RegisterClassW(&wc)==0{return Err("Native window registration failed".into())}
        let fixture=matches!(std::env::args().nth(2).as_deref(),Some("--fixture"|"--selection-fixture"));
        let hwnd=CreateWindowExW(WS_EX_TOPMOST|if fixture{WS_EX_APPWINDOW}else{WS_EX_TOOLWINDOW},name.as_ptr(),wide("屏幕翻译").as_ptr(),WS_POPUP|WS_CLIPCHILDREN,0,0,1,1,null_mut(),null_mut(),instance,null());
        if hwnd.is_null(){return Err("Native window creation failed".into())}
        if !fixture&&RegisterHotKey(hwnd,HOTKEY,MOD_CONTROL|MOD_SHIFT|MOD_NOREPEAT,b'S' as u32)==0{DestroyWindow(hwnd);return Err("Ctrl+Shift+S 已被其他应用占用，请关闭其他屏幕翻译后重试".into())}
        let (tx,rx)=mpsc::sync_channel(1);let handle=hwnd as isize;
        std::thread::spawn(move||ocr_thread(handle,runtime,rx));
        let mut buttons=Vec::new();
        for (i,label) in ["翻译选区","重新框选","重新翻译","复制译文","关闭"].iter().enumerate(){
            let button=CreateWindowExW(0,wide("BUTTON").as_ptr(),wide(label).as_ptr(),WS_CHILD|WS_TABSTOP|BS_OWNERDRAW as u32,i as i32*100,0,100,BAR,hwnd,(1001+i) as HMENU,instance,null());
            SendMessageW(button,WM_SETFONT,GetStockObject(DEFAULT_GUI_FONT) as usize,1);buttons.push(button);
        }
        let mut app=Box::new(App{hwnd,buttons,capture:None,blurred:None,backdrop:None,text_color:INK,buffer:None,selection:None,drag:None,toolbar:zeroed(),selecting:false,busy:false,original:false,segments:Vec::new(),status:String::new(),id:0,ocr:tx});
        APP=&mut *app;
        // Deterministic UI/IPC smoke fixture; never captures the user's desktop.
        if fixture {
            let path=std::env::args().nth(3).ok_or("Missing fixture image")?;
            let image=image::open(path)?.to_rgb8();let (width,height)=image.dimensions();
            let mut bgra=Vec::with_capacity((width*height*4) as usize);
            for p in image.pixels(){bgra.extend_from_slice(&[p[2],p[1],p[0],255]);}
            begin_selection(&mut app,Capture{width:width as i32,height:height as i32,left:100,top:100,bgra});
            if std::env::args().nth(2).as_deref()==Some("--fixture"){
                app.selection=Some(RECT{left:0,top:0,right:width as i32,bottom:height as i32});finish_selection(&mut app);
            }
        }
        std::thread::spawn(move||{
            for line in std::io::stdin().lock().lines(){
                match line {Ok(line) if line.len()<=2_000_000=>{if let Ok(command)=serde_json::from_str(&line){push(handle,command);}},_=>break}
            }
            push(handle,Command{kind:"shutdown".into(),..Default::default()});
        });
        emit(json!({"type":"ready","protocol":1,"hotkey":"Ctrl+Shift+S"}));
        let mut message:MSG=zeroed();
        loop {let code=GetMessageW(&mut message,null_mut(),0,0);if code<=0{break}
            if message.message==WM_KEYDOWN&&message.wParam==VK_ESCAPE as usize {close(&mut app);continue}
            if message.message==WM_KEYDOWN&&message.wParam==VK_RETURN as usize&&app.selecting {finish_selection(&mut app);continue}
            if message.message==WM_KEYDOWN&&app.selecting&&(VK_LEFT as usize..=VK_DOWN as usize).contains(&message.wParam){
                if let (Some(r),Some(frame))=(app.selection,app.capture.as_ref()){
                    let shift=GetKeyState(VK_SHIFT as i32)<0;let dx=if message.wParam==VK_LEFT as usize{-1}else if message.wParam==VK_RIGHT as usize{1}else{0};let dy=if message.wParam==VK_UP as usize{-1}else if message.wParam==VK_DOWN as usize{1}else{0};
                    app.selection=Some(dragged(Drag{point:(0,0),rect:r,edges:if shift{2|8}else{16}},(dx,dy),frame.width,frame.height));layout_toolbar(&mut app);InvalidateRect(hwnd,null(),0);
                }continue
            }
            if message.message==WM_KEYDOWN&&message.wParam==b'C' as usize&&GetKeyState(VK_CONTROL as i32)<0{copy(&app);continue}
            if IsDialogMessageW(hwnd,&message)==0 {TranslateMessage(&message);DispatchMessageW(&message);}}
        APP=null_mut();drop(app);
    }
    Ok(())
}
#[cfg(test)] mod tests {
    use super::*;
    #[test] fn selection_handles_reverse_drag_and_bounds(){let r=normalize((200,160),(-20,5),300,200);assert_eq!((r.left,r.top,r.right,r.bottom),(0,5,200,160));}
    #[test] fn resize_and_move_keep_selection_inside_screen(){
        let rect=RECT{left:20,top:30,right:220,bottom:130};
        let moved=dragged(Drag{point:(100,70),rect,edges:16},(-500,900),300,200);
        assert_eq!((moved.left,moved.top,moved.right,moved.bottom),(0,100,200,200));
        let resized=dragged(Drag{point:(20,30),rect,edges:1|4},(999,999),300,200);
        assert_eq!((resized.left,resized.top,resized.right,resized.bottom),(208,118,220,130));
        let enlarged=dragged(Drag{point:(220,130),rect,edges:2|8},(999,999),300,200);
        assert_eq!((enlarged.right,enlarged.bottom),(300,200));
        assert_eq!(hit_edges(rect,(20,30)),5);assert_eq!(hit_edges(rect,(120,80)),16);assert_eq!(hit_edges(rect,(290,190)),0);
    }
    #[test] fn uniform_frost_has_adaptive_text_and_no_per_line_blocks(){
        for (value,expected) in [(240,0x0020120B),(24,INK)]{
            let image=RgbImage::from_pixel(100,80,image::Rgb([value,value,value]));
            let (backdrop,color)=frosted_region(&image);assert_eq!(color,expected);
            let first=&backdrop.bgra[..4];assert!(backdrop.bgra.chunks_exact(4).all(|p|p==first));
            assert_eq!((backdrop.width,backdrop.height),(25,20));
        }
    }
}
