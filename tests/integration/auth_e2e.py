#!/usr/bin/env python3
"""Opt-in HTTP/SQL auth integration; exclusively the disposable auth project."""
import argparse
import base64
import concurrent.futures
import hashlib
import hmac
import http.cookiejar
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shlex
import stat
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROJECT = "phpretro-auth-dev"
ORIGIN = "http://127.0.0.1:8790"

def command(args, *, data=None, env=None, allow_failure=False):
    if env is None and args[:2] == ["docker", "compose"]:
        env = dict(os.environ, COMPOSE_PROJECT_NAME=PROJECT, WEB_PORT="8790", CMS_PUBLIC_ORIGIN=ORIGIN)
    result = subprocess.run(args, cwd=ROOT, input=data, text=True,
                            capture_output=True, env=env, timeout=120)
    if result.returncode and not allow_failure:
        raise RuntimeError("integration subprocess failed; private output withheld")
    return result

def private_env():
    path = ROOT / ".env"
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as handle:
        metadata = os.fstat(handle.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise RuntimeError("integration requires an owned mode-0600 development .env")
        values = {}
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            parsed = shlex.split(value)
            if len(parsed) > 1:
                raise RuntimeError("invalid private development configuration")
            values[key] = parsed[0] if parsed else ""
    return values

def container_info(service):
    ident = command(["docker", "compose", "-p", PROJECT, "ps", "-q", service]).stdout.strip()
    if not ident or "\n" in ident:
        raise RuntimeError("new disposable auth project must be running")
    info = json.loads(command(["docker", "inspect", ident]).stdout)[0]
    labels = info["Config"]["Labels"]
    if labels.get("com.docker.compose.project") != PROJECT or labels.get("com.docker.compose.service") != service:
        raise RuntimeError("refusing a container outside the disposable auth project")
    if Path(labels.get("com.docker.compose.project.working_dir", "")).resolve() != ROOT.resolve():
        raise RuntimeError("refusing a project outside this auth worktree")
    return ident, info

def container(service):
    ident, info = container_info(service)
    addresses = [v["IPAddress"] for v in info["NetworkSettings"]["Networks"].values() if v.get("IPAddress")]
    if len(addresses) != 1:
        raise RuntimeError("unexpected disposable database network")
    return ident, addresses[0]

def literal(value):
    return "CONVERT(X'" + str(value).encode().hex() + "' USING utf8mb4)"

def totp(secret, step):
    decoded = base64.b32decode(secret.upper() + "=" * ((-len(secret)) % 8))
    digest = hmac.new(decoded, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
    return f"{number % 1000000:06d}"

class Client:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.csrf = None

    def request(self, route, *, method="GET", payload=None, origin=True, csrf=False, raw=None, cookie=None):
        headers = {"Accept": "application/json"}
        if origin:
            headers["Origin"] = ORIGIN if origin is True else origin
        if csrf and self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        if cookie is not None:
            headers["Cookie"] = cookie
        data = None
        if payload is not None or raw is not None:
            headers["Content-Type"] = "application/json"
            data = raw if raw is not None else json.dumps(payload).encode()
        request = urllib.request.Request(ORIGIN + route, data=data, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=8)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(128 * 1024)
            try:
                parsed = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                parsed = None
            return response.status, parsed, response.headers

    def cookie(self):
        return "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.jar)

    def login(self, username, password, otp=None):
        payload = {"username": username, "password": password}
        if otp is not None:
            payload["otp"] = otp
        status, body, headers = self.request("/api/v1/auth/login", method="POST", payload=payload)
        if status == 200:
            self.csrf = body.get("csrfToken")
        return status, body, headers

def expect(label, status, expected):
    allowed = expected if isinstance(expected, tuple) else (expected,)
    if status not in allowed:
        raise AssertionError(f"{label}: status {status}, expected {allowed}")
    print("PASS", label)

def guest(label, result):
    status, body, _ = result
    expect(label, status, 200)
    if body != {"version": "session.v1", "authenticated": False}:
        raise AssertionError(label + ": invalid guest ABI")

class Fixture:
    def __init__(self, temporary):
        self.temporary = Path(temporary)
        self.values = private_env()
        self.database, self.db_address = container("mariadb")
        self.redis, _ = container("redis")
        self.ident = "it" + secrets.token_hex(6)
        self.ids = []
        self.staff_id = None
        self.original_site = None
        self.audit_table = "cms_audit_events"
        self.trigger = None
        self.operator_paths = []
        fixtures = json.loads((HERE / "java-bcrypt-fixtures.json").read_text())
        self.vector = next(row for row in fixtures["vectors"] if row["id"] == "native-cost12")

    def sql(self, query, database="phpretro_cms", *, principal="root", allow_failure=False):
        password_env = {"root": "MARIADB_ROOT_PASSWORD", "cms_app": "CMS_DB_PASSWORD", "cms_polaris_bridge": "CMS_POLARIS_DB_PASSWORD"}[principal]
        # Send the password as data on stdin; never argv, shell source or output.
        password = self.values[password_env]
        if not password or "\n" in password or "\r" in password:
            raise RuntimeError("invalid private development credential")
        script = f'IFS= read -r MYSQL_PWD; export MYSQL_PWD; exec mariadb --batch --skip-column-names -u{principal} {database}'
        return command(["docker", "exec", "-i", self.database, "sh", "-c", script], data=password+"\n"+query, allow_failure=allow_failure)

    def prepare_schema(self):
        count = self.sql("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='polaris' AND table_name IN ('users','bans');").stdout.strip()
        if count == "0":
            self.sql((HERE / "polaris-auth-schema.sql").read_text(), "polaris")
        elif count != "2":
            raise RuntimeError("refusing partial Polaris fixture schema")
        columns = self.sql("SELECT column_name,data_type,COALESCE(collation_name,'') FROM information_schema.columns WHERE table_schema='polaris' AND table_name='users' AND column_name IN ('username','account_created','access_token_version') ORDER BY column_name;").stdout.strip()
        if columns != "access_token_version\tbigint\t\naccount_created\tint\t\nusername\tvarchar\tutf8mb3_general_ci":
            raise RuntimeError("Polaris fixture schema differs from exact pinned contract")
        command(["docker","compose","-p",PROJECT,"run","--rm","--no-deps","configure-auth"])

    def seed(self, suffix, rank):
        name = self.ident + suffix
        query = f"INSERT INTO users(username,password,account_created,ip_register,ip_current,machine_id,`rank`) VALUES ({literal(name)},{literal(self.vector['hash'])},1700000000,'127.0.0.1','127.0.0.1',{literal(name)}, {rank}); SELECT LAST_INSERT_ID();"
        ident = int(self.sql(query, "polaris").stdout.strip())
        self.ids.append(ident)
        return name, ident

    def cli_env(self):
        values = self.values
        required = ("CMS_MIGRATION_DB_PASSWORD", "CMS_POLARIS_DB_PASSWORD", "CMS_STAFF_ENCRYPTION_KEY")
        if any(not values.get(key) for key in required):
            raise RuntimeError("auth development environment has not been provisioned")
        environment = os.environ.copy()
        environment.update(CMS_MIGRATION_DSN=f"cms_migrator:{values['CMS_MIGRATION_DB_PASSWORD']}@tcp({self.db_address}:3306)/phpretro_cms?parseTime=true&charset=utf8mb4", POLARIS_DATABASE_DSN=f"cms_polaris_bridge:{values['CMS_POLARIS_DB_PASSWORD']}@tcp({self.db_address}:3306)/polaris?parseTime=true&charset=utf8mb4", CMS_STAFF_ENCRYPTION_KEY=values['CMS_STAFF_ENCRYPTION_KEY'])
        return environment

    def build_and_enroll(self, ident):
        self.binary = self.temporary / "cms"
        command(["go", "build", "-o", str(self.binary), "./cmd/cms"])
        path = self.temporary / "enrollment.json"
        command([str(self.binary), "--grant-admin", str(ident), "--enrollment-file", str(path)], env=self.cli_env())
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise AssertionError("enrollment file is not mode0600")
        record = json.loads(path.read_text())
        if record.get("version") != "staff-enrollment.v1" or record.get("userId") != ident or not record.get("secret") or not record.get("otpauthUri"):
            raise AssertionError("enrollment ABI mismatch")
        before = self.sql(f"SELECT mfa_revision FROM cms_staff_roles WHERE user_id={ident};").stdout
        duplicate = command([str(self.binary), "--grant-admin", str(ident), "--enrollment-file", str(path)], env=self.cli_env(), allow_failure=True)
        if duplicate.returncode == 0 or json.loads(path.read_text()) != record:
            raise AssertionError("existing enrollment file was overwritten")
        if before != self.sql(f"SELECT mfa_revision FROM cms_staff_roles WHERE user_id={ident};").stdout:
            raise AssertionError("failed enrollment changed MFA revision")
        self.staff_id = ident
        print("PASS operator enrollment and existing-file preservation")
        return record["secret"]

    def ban(self, ident, kind, expiry):
        _, web = container_info("web")
        networks = web["NetworkSettings"]["Networks"]
        peer = networks[PROJECT + "_internal"]["IPAddress"]
        trusted = self.values.get("CMS_TRUSTED_PROXY_CIDRS", "")
        if any(ipaddress.ip_address(peer) in ipaddress.ip_network(cidr.strip()) for cidr in trusted.split(",") if cidr.strip()):
            peer = networks[PROJECT + "_edge"]["Gateway"]
        self.sql(f"INSERT INTO bans(user_id,ip,machine_id,user_staff_id,`timestamp`,ban_expire,ban_reason,type) SELECT id,{literal(peer)},machine_id,0,UNIX_TIMESTAMP(),{expiry},{literal(self.ident)},{literal(kind)} FROM users WHERE id={ident};", "polaris")

    def clear_bans(self):
        self.sql(f"DELETE FROM bans WHERE ban_reason={literal(self.ident)};", "polaris")

    def clear_redis(self):
        # This container belongs exclusively to the disposable auth fixture.
        command(["docker", "exec", self.redis, "sh", "-c", 'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli FLUSHDB'], data=None)

    def clear_limits(self):
        script = "local c='0';repeat local v=redis.call('SCAN',c,'MATCH','phpretro:login:*','COUNT',100);c=v[1];for _,k in ipairs(v[2]) do redis.call('DEL',k) end until c=='0';return 1"
        command(["docker","exec",self.redis,"sh","-c",'export REDISCLI_AUTH="$REDIS_PASSWORD"; exec redis-cli "$@"',"redis-fixture","EVAL",script,"0"])

    def session_exists(self, cookie):
        tokens = [part.split("=",1)[1] for part in cookie.split("; ") if "=" in part]
        if len(tokens) != 1:
            raise AssertionError("fixture expected one opaque session cookie")
        key = "phpretro:session:" + hashlib.sha256(("0:0:"+tokens[0]).encode()).hexdigest()
        result = command(["docker","exec",self.redis,"sh","-c",'export REDISCLI_AUTH="$REDIS_PASSWORD"; exec redis-cli "$@"',"redis-fixture","EXISTS",key])
        return result.stdout.strip() == "1"

    def invalid_staff_session(self, label, client):
        old = client.cookie()
        if not self.session_exists(old):
            raise AssertionError(label+": fixture staff session was absent before invalidation")
        result = client.request("/api/v1/auth/session")
        guest(label,result)
        if client.cookie() or "max-age=0" not in result[2].get("Set-Cookie","").lower():
            raise AssertionError(label+": cookie not cleared")
        if self.session_exists(old):
            raise AssertionError(label+": Redis session not deleted")
        expect(label+" housekeeping denied",client.request("/api/v1/housekeeping/site")[0],401)
        print("PASS "+label+" clears cookie and deletes Redis session")
        return old

    def fresh_mfa_login(self, client, name, password, secret, ident):
        last = int(self.sql(f"SELECT last_totp_step FROM cms_staff_roles WHERE user_id={ident};").stdout.strip())
        deadline = time.monotonic() + 35
        while last + 1 > int(time.time()) // 30 + 1:
            if time.monotonic() > deadline:
                raise AssertionError("bounded TOTP window wait exceeded")
            time.sleep(0.25)
        step = max(last + 1, int(time.time()) // 30)
        code = totp(secret,step)
        status,view,_ = client.login(name,password,code)
        expect("fresh staff MFA login",status,200)
        if view["user"]["role"] != "admin":
            raise AssertionError("fresh MFA did not regain admin")
        return step,code

    def reenroll_regression(self, admin, name, password, secret, ident):
        before = self.sql(f"SELECT role_revision,mfa_revision FROM cms_staff_roles WHERE user_id={ident};").stdout.strip().split("\t")
        removed = Client()
        self.clear_limits()
        self.fresh_mfa_login(removed,name,password,secret,ident)
        command([str(self.binary),"--revoke-admin",str(ident)],env=self.cli_env())
        revoked_cookie = self.invalid_staff_session("removed staff session",removed)
        directory = ROOT / ".builder" / "enrollment"
        if not directory.exists():
            directory.mkdir(parents=True,mode=0o700)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700:
            raise RuntimeError("operator enrollment directory must be private and owned")
        path = directory / (self.ident+"-reenrollment.json")
        self.operator_paths.append(path)
        environment = dict(os.environ,COMPOSE_PROJECT_NAME=PROJECT,WEB_PORT="8790",CMS_PUBLIC_ORIGIN=ORIGIN,LOCAL_UID=str(os.getuid()),LOCAL_GID=str(os.getgid()))
        command(["docker","compose","-p",PROJECT,"--profile","ops","run","--rm","--no-deps","operator","--grant-admin",str(ident),"--enrollment-file","/enrollment/"+path.name],env=environment)
        metadata = path.stat()
        if metadata.st_uid != os.getuid() or metadata.st_gid != os.getgid() or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise AssertionError("container enrollment ownership/mode mismatch")
        record = json.loads(path.read_text())
        if record.get("version") != "staff-enrollment.v1" or record.get("userId") != ident or record.get("secret") == secret:
            raise AssertionError("container re-enrollment did not create fresh credentials")
        after = self.sql(f"SELECT role_revision,mfa_revision FROM cms_staff_roles WHERE user_id={ident};").stdout.strip().split("\t")
        if before[0] != after[0] or before[1] == after[1]:
            raise AssertionError("same-role-revision/fresh-MFA fixture not established")
        prior = self.invalid_staff_session("re-enrolled old staff session",admin)
        guest("removed staff cookie cannot revive",Client().request("/api/v1/auth/session",cookie=revoked_cookie))
        guest("old MFA staff cookie cannot revive",Client().request("/api/v1/auth/session",cookie=prior))
        new_secret = record["secret"]
        step = int(time.time())//30
        new_codes = {totp(new_secret,step+offset) for offset in (-1,0,1)}
        old_code = next(totp(secret,step+offset) for offset in (-1,0,1) if totp(secret,step+offset) not in new_codes)
        self.clear_limits()
        expect("prior MFA secret denied after re-enrollment",Client().login(name,password,old_code)[0],401)
        self.fresh_mfa_login(admin,name,password,new_secret,ident)
        print("PASS container re-enrollment0600, unchanged role revision, fresh MFA revision and new-secret authority")
        return new_secret

    def outage_and_replay(self, admin, name, password, secret, ident):
        step,code = self.fresh_mfa_login(admin,name,password,secret,ident)
        command(["docker","stop",self.redis])
        try:
            expect("Redis outage denies login",Client().login(name,password,code)[0],503)
            expect("Redis outage denies session",admin.request("/api/v1/auth/session")[0],503)
            expect("Redis outage denies housekeeping",admin.request("/api/v1/housekeeping/site")[0],503)
        finally:
            command(["docker","start",self.redis])
        for _ in range(40):
            result = command(["docker","exec",self.redis,"sh","-c",'REDISCLI_AUTH="$REDIS_PASSWORD" redis-cli ping'],allow_failure=True)
            if result.returncode == 0 and result.stdout.strip() == "PONG":
                break
            time.sleep(0.25)
        else:
            raise AssertionError("disposable Redis did not restart")
        if abs(int(time.time()) // 30 - step) > 1:
            raise AssertionError("replay fixture expired before durability could be verified")
        expect("durable TOTP replay survives Redis restart",Client().login(name,password,code)[0],401)

    def privileges(self, ident):
        if self.sql(f"SELECT id FROM users WHERE id={ident};","polaris",principal="cms_polaris_bridge").stdout.strip() != str(ident):
            raise AssertionError("bridge positive read probe failed")
        denied = self.sql(f"UPDATE users SET username=username WHERE id={ident};","polaris",principal="cms_polaris_bridge",allow_failure=True)
        if denied.returncode == 0 or "denied" not in denied.stderr.lower():
            raise AssertionError("Polaris bridge is writable")
        denied = self.sql(f"SELECT mail FROM users WHERE id={ident};","polaris",principal="cms_polaris_bridge",allow_failure=True)
        if denied.returncode == 0 or "denied" not in denied.stderr.lower():
            raise AssertionError("bridge can read columns outside identity contract")
        self.sql(f"UPDATE cms_staff_roles SET last_totp_step=last_totp_step WHERE user_id={ident};",principal="cms_app")
        denied = self.sql(f"UPDATE cms_staff_roles SET role_revision=role_revision WHERE user_id={ident};",principal="cms_app",allow_failure=True)
        if denied.returncode == 0 or "denied" not in denied.stderr.lower():
            raise AssertionError("runtime can update role authority")
        print("PASS read-only Polaris bridge and staff column grant boundary")

    def audit_rollback(self, admin, public):
        before = public.request("/api/v1/site")[1]
        before_audit = self.sql(f"SELECT COUNT(*) FROM `{self.audit_table}` WHERE actor_user_id={self.staff_id};").stdout
        self.trigger = self.ident + "_audit_fail"
        self.sql(f"CREATE TRIGGER `{self.trigger}` BEFORE INSERT ON `{self.audit_table}` FOR EACH ROW SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT='integration audit rollback';")
        update = {"version":"site-update.v1","siteName":"Must roll back", "activeTheme":"modern","defaultLocale":"en","enabledLocales":["en","nl"],"expectedRevision":before["settingsRevision"]}
        try:
            expect("audit failure denies mutation",admin.request("/api/v1/housekeeping/site",method="PUT",payload=update,csrf=True)[0],503)
        finally:
            self.sql(f"DROP TRIGGER `{self.trigger}`;")
            self.trigger = None
        after = public.request("/api/v1/site")[1]
        after_audit = self.sql(f"SELECT COUNT(*) FROM `{self.audit_table}` WHERE actor_user_id={self.staff_id};").stdout
        if after != before or before_audit != after_audit:
            raise AssertionError("settings or audit changed despite failed audit transaction")
        print("PASS audit transaction rollback preserves settings and revision")

    def cleanup(self):
        for path in self.operator_paths:
            path.unlink(missing_ok=True)
        if self.trigger:
            self.sql(f"DROP TRIGGER IF EXISTS `{self.trigger}`;", allow_failure=True)
        self.clear_bans()
        if self.ids:
            ids = ",".join(str(value) for value in self.ids)
            if self.audit_table:
                self.sql(f"DELETE FROM `{self.audit_table}` WHERE actor_user_id IN ({ids}) OR (actor_user_id=0 AND action IN ('staff.enroll','staff.revoke') AND (JSON_EXTRACT(before_json,'$.userId') IN ({ids}) OR JSON_EXTRACT(after_json,'$.userId') IN ({ids})));", allow_failure=True)
            self.sql(f"DELETE FROM cms_staff_roles WHERE user_id IN ({ids});", allow_failure=True)
            self.sql(f"DELETE FROM users WHERE id IN ({ids}) AND username LIKE {literal(self.ident + '%')};", "polaris", allow_failure=True)
        if self.original_site:
            site = self.original_site
            self.sql(f"UPDATE cms_settings SET site_name={literal(site['siteName'])},active_theme={literal(site['activeTheme'])},default_locale={literal(site['defaultLocale'])},enabled_locales={literal(json.dumps(site['enabledLocales']))},revision={int(site['settingsRevision'])} WHERE id=1;", allow_failure=True)

def run(fixture):
    fixture.prepare_schema()
    member_name, member_id = fixture.seed("Member", 99)
    admin_name, admin_id = fixture.seed("Admin", 1)
    secret = fixture.build_and_enroll(admin_id)
    password = fixture.vector["password"]
    public, member, admin = Client(), Client(), Client()
    guest("anonymous session", public.request("/api/v1/auth/session"))
    expect("missing login origin", public.request("/api/v1/auth/login", method="POST", origin=False, payload={"username":member_name,"password":password})[0], 403)
    expect("wrong credentials", public.login(member_name,"wrong-fixture-password")[0],401)
    expect("unknown identity", public.login(fixture.ident+"Missing",password)[0],401)
    expect("bcrypt UTF8 byte limit", public.login(member_name,"é"*37)[0],401)
    status, _, cookie_headers = member.login(member_name.lower(),password)
    expect("member login with case-insensitive identity",status,200)
    cookie_header = cookie_headers.get("Set-Cookie","").lower()
    if "httponly" not in cookie_header or "samesite=lax" not in cookie_header or "path=/" not in cookie_header or "domain=" in cookie_header:
        raise AssertionError("session cookie policy mismatch")
    status, body, headers = member.request("/api/v1/auth/session")
    expect("member session",status,200)
    if body["user"]["id"] != member_id or body["user"]["role"] != "member" or body["user"]["username"] != member_name:
        raise AssertionError("Polaris rank incorrectly grants CMS role or canonical identity lost")
    replaced = member.cookie()
    expect("replacement login",member.login(member_name,password)[0],200)
    guest("replacement login retires old cookie",Client().request("/api/v1/auth/session",cookie=replaced))
    expect("member denied housekeeping",member.request("/api/v1/housekeeping/site")[0],403)
    for kind in ("account","ip","machine","super"):
        fixture.ban(member_id,kind,int(time.time())+120)
        expect(kind+" ban denies login",Client().login(member_name,password)[0],401)
        guest(kind+" ban revokes existing session",member.request("/api/v1/auth/session"))
        fixture.clear_bans()
        fixture.clear_redis()
        expect("login after "+kind+" ban removed",member.login(member_name,password)[0],200)
    for expiry in (0,int(time.time())-60):
        fixture.ban(member_id,"account",expiry)
        expect("expired ban permits login",member.login(member_name,password)[0],200)
        fixture.clear_bans()
    stale = member.cookie()
    fixture.sql(f"UPDATE users SET access_token_version=access_token_version+1 WHERE id={member_id};","polaris")
    guest("token version revokes session",member.request("/api/v1/auth/session"))
    guest("old cookie remains revoked",Client().request("/api/v1/auth/session",cookie=stale))
    expect("member login after version change",member.login(member_name,password)[0],200)
    retired = member.cookie()
    other_hash = next(row["hash"] for row in json.loads((HERE/"java-bcrypt-fixtures.json").read_text())["vectors"] if row["id"] == "unicode-utf8")
    fixture.sql(f"UPDATE users SET password={literal(other_hash)} WHERE id={member_id};","polaris")
    guest("password binding revokes session",member.request("/api/v1/auth/session"))
    fixture.sql(f"UPDATE users SET password={literal(fixture.vector['hash'])} WHERE id={member_id};","polaris")
    guest("revoked password cookie cannot revive",Client().request("/api/v1/auth/session",cookie=retired))
    fixture.clear_redis()
    for column in ("account_created","access_token_version"):
        original = int(fixture.sql(f"SELECT `{column}` FROM users WHERE id={member_id};","polaris").stdout.strip())
        fixture.sql(f"UPDATE users SET `{column}`=-1 WHERE id={member_id};","polaris")
        expect("negative "+column+" denied",Client().login(member_name,password)[0],401)
        fixture.sql(f"UPDATE users SET `{column}`={original} WHERE id={member_id};","polaris")
    for broken_hash in ("malformed",fixture.vector["hash"].replace("$12$","$31$",1)):
        fixture.sql(f"UPDATE users SET password={literal(broken_hash)} WHERE id={member_id};","polaris")
        expect("invalid or excessive-cost bcrypt denied",Client().login(member_name,password)[0],401)
    fixture.sql(f"UPDATE users SET password={literal(fixture.vector['hash'])} WHERE id={member_id};","polaris")
    expect("staff login requires MFA",admin.login(admin_name,password)[0],401)
    expect("malformed MFA denied",admin.login(admin_name,password,"invalid")[0],401)
    step = int(time.time())//30
    current = {totp(secret,step+offset) for offset in (-1,0,1)}
    expired = next(totp(secret,step-offset) for offset in range(5,20) if totp(secret,step-offset) not in current)
    expect("expired MFA denied",admin.login(admin_name,password,expired)[0],401)
    fixture.clear_redis()
    step = int(time.time())//30
    code = totp(secret,step)
    def concurrent_mfa(_):
        candidate = Client()
        return candidate.login(admin_name,password,code)[0], candidate
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(concurrent_mfa,(1,2)))
    if sorted(status for status,_ in attempts) != [200,401]:
        raise AssertionError("concurrent TOTP compare-and-set did not admit exactly one session")
    admin = next(candidate for status,candidate in attempts if status == 200)
    if int(fixture.sql(f"SELECT last_totp_step FROM cms_staff_roles WHERE user_id={admin_id};").stdout.strip()) != step:
        raise AssertionError("accepted TOTP step was not persisted")
    print("PASS concurrent TOTP compare-and-set admits one session and persists its step")
    expect("TOTP replay denied",Client().login(admin_name,password,code)[0],401)
    expect("member session after fixture cache reset",member.login(member_name,password)[0],200)
    fixture.privileges(admin_id)
    status, initial, _ = public.request("/api/v1/site")
    expect("persisted public settings",status,200); fixture.original_site = initial
    expect("admin housekeeping read",admin.request("/api/v1/housekeeping/site")[0],200)
    update = {"version":"site-update.v1","siteName":"Integration fixture", "activeTheme":"legacy","defaultLocale":"nl","enabledLocales":["en","nl"],"expectedRevision":initial["settingsRevision"]}
    expect("member mutation denied",member.request("/api/v1/housekeeping/site",method="PUT",payload=update,csrf=True)[0],403)
    expect("missing CSRF denied",admin.request("/api/v1/housekeeping/site",method="PUT",payload=update)[0],403)
    expect("wrong mutation origin denied",admin.request("/api/v1/housekeeping/site",method="PUT",payload=update,csrf=True,origin="https://invalid.example")[0],403)
    for raw in (json.dumps(dict(update,unexpected=True)).encode(), json.dumps(update).encode()+b' {}', b'x'*(128*1024)):
        expect("strict mutation JSON",admin.request("/api/v1/housekeeping/site",method="PUT",raw=raw,csrf=True)[0],(400,413))
    expect("admin persists settings",admin.request("/api/v1/housekeeping/site",method="PUT",payload=update,csrf=True)[0],200)
    status, persisted, _ = public.request("/api/v1/site")
    if status != 200 or persisted["activeTheme"] != "legacy" or persisted["defaultLocale"] != "nl" or persisted["settingsRevision"] != initial["settingsRevision"]+1:
        raise AssertionError("public settings did not persist")
    print("PASS public persisted theme, locale and revision")
    update["expectedRevision"] = persisted["settingsRevision"]
    def concurrent_update(index):
        client = Client(); client.csrf = admin.csrf
        return client.request("/api/v1/housekeeping/site",method="PUT",payload=dict(update,siteName="Concurrent fixture "+str(index)),csrf=True,cookie=admin.cookie())[0]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        codes = list(pool.map(concurrent_update,(1,2)))
    if sorted(codes) != [200,409]:
        raise AssertionError("concurrent settings update did not produce one commit and one conflict")
    print("PASS concurrent revision conflict")
    if fixture.sql(f"SELECT COUNT(*) FROM cms_audit_events WHERE actor_user_id={admin_id};").stdout.strip() != "2":
        raise AssertionError("successful changes did not produce exactly two durable audit events")
    fixture.audit_rollback(admin,public)
    secret = fixture.reenroll_regression(admin,admin_name,password,secret,admin_id)
    fixture.sql(f"UPDATE cms_staff_roles SET role_revision=role_revision+1 WHERE user_id={admin_id};")
    fixture.invalid_staff_session("live role revision invalidates session",admin)
    fixture.outage_and_replay(admin,admin_name,password,secret,admin_id)
    expect("member login after fixture Redis restart",member.login(member_name,password)[0],200)
    expect("logout missing CSRF denied",member.request("/api/v1/auth/logout",method="POST",payload={})[0],403)
    expect("logout",member.request("/api/v1/auth/logout",method="POST",payload={},csrf=True)[0],200)
    guest("logout clears session",member.request("/api/v1/auth/session"))
    step = int(fixture.sql(f"SELECT last_totp_step FROM cms_staff_roles WHERE user_id={admin_id};").stdout.strip())
    if abs(int(time.time())//30-step) <= 1:
        expect("role revision does not reset replay ledger",admin.login(admin_name,password,totp(secret,step))[0],401)
    command([str(fixture.binary),"--revoke-admin",str(admin_id)],env=fixture.cli_env())
    expect("revoked staff becomes member",Client().login(admin_name,password)[0],200)
    fixture.clear_redis()
    aliases = [prefix+member_name[1:] for prefix in ("i","í","ì","î","ï","ī","ĭ","ĩ","į")]
    for index, alias in enumerate(aliases):
        if fixture.sql(f"SELECT id FROM users WHERE username={literal(alias)};","polaris").stdout.strip() != str(member_id):
            raise AssertionError("accent fixture is not collation-equivalent")
        expect("canonical account rate bucket attempt "+str(index+1),Client().login(alias,"wrong-fixture-password")[0],429 if index == 8 else 401)
    expect("rate limit blocks correct password too",Client().login(member_name,password)[0],429)
    fixture.clear_redis()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute",action="store_true",help="run against the new, disposable phpretro-auth-dev project only")
    parser.add_argument("--prepare-schema",action="store_true",help="create exact users/bans fixture DDL when both are absent; no users are seeded")
    args = parser.parse_args()
    if not (args.execute or args.prepare_schema):
        parser.error("choose explicit --prepare-schema or --execute")
    with tempfile.TemporaryDirectory(prefix="phpretro-auth-it-") as temporary:
        fixture = Fixture(temporary)
        if args.prepare_schema:
            fixture.prepare_schema(); print("PASS exact pinned Polaris fixture DDL; no accounts copied")
        if args.execute:
            try:
                run(fixture)
            finally:
                fixture.cleanup()

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        message = str(error) if isinstance(error,AssertionError) else type(error).__name__ + " (private response/configuration output withheld)"
        print("FAIL",message)
        raise SystemExit(1)
