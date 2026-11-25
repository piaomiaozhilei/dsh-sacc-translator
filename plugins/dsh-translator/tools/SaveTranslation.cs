using System;
using System.IO;
using System.Diagnostics;
using System.Windows.Forms;
using System.Web.Script.Serialization;

public static class SaveTranslation {
    static void Emit(object value) { Console.WriteLine(new JavaScriptSerializer().Serialize(value)); Console.Out.Flush(); }
    public static void CopySelected(string source, string destination, Action<long,long> progress) {
        if(String.Equals(Path.GetFullPath(source),Path.GetFullPath(destination),StringComparison.OrdinalIgnoreCase)) throw new IOException("不能覆盖原始译文文件，请选择其他位置。");
        string temporary=Path.Combine(Path.GetDirectoryName(destination),".dsh-save-"+Guid.NewGuid().ToString("N")+".tmp");
        try {
            using(var input=new FileStream(source,FileMode.Open,FileAccess.Read,FileShare.Read,1048576,FileOptions.SequentialScan))
            using(var output=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write,FileShare.None,1048576,FileOptions.SequentialScan)) {
                var buffer=new byte[1048576];long copied=0;int count;var timer=Stopwatch.StartNew();
                progress(0,input.Length);
                while((count=input.Read(buffer,0,buffer.Length))!=0) {
                    output.Write(buffer,0,count);copied+=count;
                    if(timer.ElapsedMilliseconds>=250) {progress(copied,input.Length);timer.Restart();}
                }
                output.Flush(true);progress(copied,input.Length);
            }
            if(File.Exists(destination)) File.Replace(temporary,destination,null);
            else File.Move(temporary,destination);
        } finally { if(File.Exists(temporary))File.Delete(temporary); }
    }
    [STAThread] public static int Main(string[] args) {
        try {
            if(args.Length!=2)throw new ArgumentException("Invalid save request");
            Application.EnableVisualStyles();
            using(var dialog=new SaveFileDialog()) {
                dialog.Title="保存译文";dialog.FileName=Path.GetFileName(args[1]);
                dialog.OverwritePrompt=true;dialog.CheckPathExists=true;dialog.RestoreDirectory=true;
                dialog.Filter="译文文件 (*"+Path.GetExtension(dialog.FileName)+")|*"+Path.GetExtension(dialog.FileName)+"|所有文件 (*.*)|*.*";
                Emit(new {status="choosing"});
                if(dialog.ShowDialog()!=DialogResult.OK) {Emit(new {status="cancelled"});return 0;}
                // Never open the source until the user accepts the save dialog.
                CopySelected(args[0],dialog.FileName,(bytes,total)=>Emit(new {status="saving",bytes,total}));
                Emit(new {status="saved",path=dialog.FileName});
            }
            return 0;
        } catch(Exception e) {Emit(new {status="failed",error=e.Message});return 1;}
    }
}
