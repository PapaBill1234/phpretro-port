#!/usr/bin/env python3
"""Check Java->Go and Go->Java bcrypt compatibility in disposable probes."""
import json
import os
from pathlib import Path
import subprocess
import tempfile

import regenerate_bcrypt_fixture as fixture

GO = r'''
package main
import("encoding/json";"fmt";"os";"golang.org/x/crypto/bcrypt")
type Vector struct { ID string `json:"id"`; Password string `json:"password"`; Hash string `json:"hash"` }
func main(){
 f,e:=os.Open(os.Args[1]);if e!=nil{panic("fixture unavailable")};defer f.Close()
 var input struct { Vectors []Vector `json:"vectors"` }
 if json.NewDecoder(f).Decode(&input)!=nil||len(input.Vectors)!=4{panic("invalid fixture")}
 for _,v:=range input.Vectors{
  if bcrypt.CompareHashAndPassword([]byte(v.Hash),[]byte(v.Password))!=nil{panic("Java hash rejected by Go")}
  wrong:=[]byte(v.Password);wrong[0]='z';if bcrypt.CompareHashAndPassword([]byte(v.Hash),wrong)==nil{panic("wrong password accepted")}
 }
 hash,e:=bcrypt.GenerateFromPassword([]byte("go-to-java-fixture"),4);if e!=nil{panic("Go hashing failed")}
 fmt.Print(string(hash))
}
'''
JAVA = r'''
import com.eu.habbo.networking.gameserver.auth.PasswordHasher;
import java.nio.file.Files;
import java.nio.file.Path;
public class ReverseProbe {
 public static void main(String[] ignored) throws Exception {
  String hash=Files.readString(Path.of("go-hash.txt"));
  if(!PasswordHasher.verify("go-to-java-fixture",hash)||PasswordHasher.verify("wrong-fixture",hash))
   throw new IllegalStateException("Go hash rejected by Java");
 }
}
'''

def main():
    with tempfile.TemporaryDirectory(prefix="phpretro-bcrypt-interop-") as scratch:
        tmp = Path(scratch)
        (tmp / "go.mod").write_text("module phpretro-bcrypt-probe\n\ngo 1.26.0\n\nrequire golang.org/x/crypto v0.58.0\n")
        (tmp / "main.go").write_text(GO)
        result = subprocess.run(["go", "run", "-mod=mod", ".", str(fixture.ROOT / "java-bcrypt-fixtures.json")], cwd=tmp, capture_output=True, text=True, timeout=120)
        if result.returncode or not result.stdout.startswith("$2a$"):
            raise RuntimeError("Go bcrypt fixture verification failed")
        (tmp / "go-hash.txt").write_text(result.stdout)
        for artifact, (version, digest) in fixture.ARTIFACTS.items():
            url = f"https://repo.maven.apache.org/maven2/at/favre/lib/{artifact}/{version}/{artifact}-{version}.jar"
            (tmp / f"{artifact}.jar").write_bytes(fixture.download(url, digest))
        (tmp / "PasswordHasher.java").write_bytes(fixture.download(f"https://raw.githubusercontent.com/duckietm/Polaris-Emulator/{fixture.POLARIS_SHA}/{fixture.SOURCE}", fixture.SOURCE_SHA256))
        (tmp / "ReverseProbe.java").write_text(JAVA)
        result = subprocess.run(["docker","run","--rm","--network","none","--read-only","--cap-drop","ALL","--security-opt","no-new-privileges","--tmpfs","/tmp:rw,nosuid,nodev","--user",f"{os.getuid()}:{os.getgid()}","-v",f"{tmp}:/probe:rw","-w","/probe",fixture.IMAGE,"sh","-c","javac -cp 'bcrypt.jar:bytes.jar' -d . PasswordHasher.java ReverseProbe.java && java -cp '.:bcrypt.jar:bytes.jar' ReverseProbe"],capture_output=True,text=True,timeout=90)
        if result.returncode:
            raise RuntimeError("reverse Java bcrypt verification failed")
        print("PASS Java->Go four vectors and Go->Java generated hash (x/crypto v0.58.0, favre0.10.2).")

if __name__ == "__main__":
    main()
