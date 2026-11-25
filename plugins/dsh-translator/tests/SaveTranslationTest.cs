using System;
using System.IO;
using System.Diagnostics;
using System.Security.Cryptography;
public static class SaveTranslationTest {
    static string Hash(string path){using(var f=File.OpenRead(path))using(var sha=SHA256.Create())return Convert.ToBase64String(sha.ComputeHash(f));}
    public static void Main(string[] args){
        var root=Path.GetFullPath(args[0]);Directory.CreateDirectory(root);
        var source=Path.Combine(root,"synthetic-source.bin");var target=Path.Combine(root,"synthetic-saved.bin");
        var buffer=new byte[1048576];new Random(1234).NextBytes(buffer);
        using(var f=File.Create(source))for(int i=0;i<256;i++)f.Write(buffer,0,buffer.Length);
        int updates=0;var sw=Stopwatch.StartNew();
        SaveTranslation.CopySelected(source,target,(bytes,total)=>{if(total!=268435456)throw new Exception("size");updates++;});sw.Stop();
        if(Hash(source)!=Hash(target)||updates<2)throw new Exception("copy verification failed");
        Console.WriteLine("Synthetic 256 MiB: "+sw.Elapsed.TotalSeconds.ToString("F3")+" s; "+(256/sw.Elapsed.TotalSeconds).ToString("F1")+" MiB/s; SHA256 matched");
        var before=Hash(target);
        try{SaveTranslation.CopySelected(source+".missing",target,(a,b)=>{});throw new Exception("missing input accepted");}catch(FileNotFoundException){}
        if(Hash(target)!=before)throw new Exception("existing destination changed after failure");
        SaveTranslation.CopySelected(source,target,(a,b)=>{});
        if(Hash(target)!=before)throw new Exception("replacement mismatch");
        if(Directory.GetFiles(root,".dsh-save-*.tmp").Length!=0)throw new Exception("temporary file leaked");
        Console.WriteLine("PASS: stream copy, confirmed replacement, failure preserves destination, temporary cleanup");
    }
}
