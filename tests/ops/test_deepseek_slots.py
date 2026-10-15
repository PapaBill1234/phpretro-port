import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import json

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/deepseek-slots.py"
spec = importlib.util.spec_from_file_location("deepseek_slots", SCRIPT)
slots = importlib.util.module_from_spec(spec)
spec.loader.exec_module(slots)


class SlotTests(unittest.TestCase):
    def test_four_separate_processes_admit_and_fifth_denies(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "slots"
            holders = []
            try:
                for _ in range(4):
                    child = subprocess.Popen([sys.executable, "-c",
                        "import importlib.util,sys,time,signal;from pathlib import Path;"
                        "s=importlib.util.spec_from_file_location('s',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                        "fd=m.reserve(Path(sys.argv[2]));signal.signal(signal.SIGTERM,lambda *x:(m.lease_write(fd,{'phase':'finished'}),sys.exit(0)));print('ready',flush=True);time.sleep(30)", str(SCRIPT), str(state)], stdout=subprocess.PIPE, text=True)
                    holders.append(child)
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                with self.assertRaisesRegex(ValueError, "Four direct"):
                    slots.reserve(state)
                holders[0].terminate(); holders[0].wait(timeout=5)
                fd = slots.reserve(state)
                slots.os.close(fd)
            finally:
                for child in holders:
                    child.terminate(); child.wait(timeout=5); child.stdout.close()

    def test_slot_is_retained_by_actual_child_when_supervisor_dies(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "slots"
            ready = Path(tmp) / "child.pid"
            parent = subprocess.Popen([sys.executable, "-c",
                "import importlib.util,sys;from pathlib import Path;"
                "s=importlib.util.spec_from_file_location('s',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "sys.exit(m.run(Path(sys.argv[2]),[sys.executable,'-c',\"import os,time,pathlib;pathlib.Path(\"+repr(sys.argv[3])+\").write_text(str(os.getpid()));time.sleep(30)\"],window={'stop_at':'2099-01-01T00:00:00+00:00'}))",
                str(SCRIPT), str(state), str(ready)])
            child_pid = None
            try:
                end = time.monotonic() + 20
                while (not ready.exists() or not ready.read_text().strip()) and time.monotonic() < end:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                child_pid = int(ready.read_text())
                parent.kill(); parent.wait(timeout=5)
                held = [slots.reserve(state) for _ in range(3)]
                try:
                    with self.assertRaisesRegex(ValueError, "Four direct"):
                        slots.reserve(state)
                finally:
                    for fd in held: slots.os.close(fd)
            finally:
                if parent.poll() is None: parent.kill(); parent.wait(timeout=5)
                if child_pid:
                    try: slots.os.killpg(child_pid, slots.signal.SIGKILL)
                    except ProcessLookupError: pass

    def test_symlink_slot_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "slots"; directory.mkdir(mode=0o700)
            target = Path(tmp) / "other"; target.write_text("unchanged")
            (directory / "0.lock").symlink_to(target)
            with self.assertRaises(OSError): slots.reserve(directory)
            self.assertEqual(target.read_text(), "unchanged")

    def test_atomic_update_failure_retains_reserved_lease_and_blocks_fifth_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'slots'
            fd = slots.reserve(directory)
            metadata = slots.lease_path(fd)
            before = metadata.read_bytes()
            try:
                with patch.object(slots.os, 'replace', side_effect=OSError('injected interruption before publication')):
                    with self.assertRaises(OSError):
                        slots.lease_write(fd, {'phase':'running','group':123456789,'provider':'direct-deepseek'})
                self.assertEqual(metadata.read_bytes(), before)
                self.assertEqual(json.loads(before)['phase'],'reserved')
            finally: slots.os.close(fd)
            held = [slots.reserve(directory) for _ in range(3)]
            try:
                with self.assertRaisesRegex(ValueError,'Four direct'):
                    slots.reserve(directory)
            finally:
                for item in held: slots.os.close(item)

    def test_existing_empty_or_truncated_metadata_never_becomes_free(self):
        for content in ('', '{"phase":'):
            with tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / 'slots'; directory.mkdir(mode=0o700)
                slot = directory / '0.lock'; slot.write_text(content); slot.chmod(0o600)
                with self.assertRaises(ValueError): slots.reserve(directory)
                self.assertEqual(slot.read_text(),content)

    def test_surviving_native_grandchild_keeps_group_lease_after_adapter_and_supervisor_die(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "slots"; ready = Path(tmp) / "native.json"
            js = "const{spawn}=require('child_process');const fs=require('fs');const c=spawn(process.execPath,['-e','setInterval(()=>{},1000)'],{stdio:'ignore'});fs.writeFileSync(process.argv[1],JSON.stringify({adapter:process.pid,native:c.pid}));setInterval(()=>{},1000)"
            parent = subprocess.Popen([sys.executable, "-c",
                "import importlib.util,sys;from pathlib import Path;"
                "s=importlib.util.spec_from_file_location('s',sys.argv[1]);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "sys.exit(m.run(Path(sys.argv[2]),['node','-e',sys.argv[3],sys.argv[4]],window={'stop_at':'2099-01-01T00:00:00+00:00'}))", str(SCRIPT), str(state), js, str(ready)])
            group = None; held = []
            try:
                end = time.monotonic() + 20
                while (not ready.exists() or not ready.read_text().strip()) and time.monotonic() < end: time.sleep(0.02)
                value = json.loads(ready.read_text()); group = value['adapter']
                parent.kill(); parent.wait(timeout=5)
                slots.os.kill(value['adapter'], slots.signal.SIGKILL)
                time.sleep(0.05)
                self.assertTrue(slots.group_alive(group))
                held = [slots.reserve(state) for _ in range(3)]
                with self.assertRaisesRegex(ValueError, 'Four direct'):
                    slots.reserve(state)
            finally:
                if group is None and state.exists():
                    for lease in state.glob('*.lock.json'):
                        try: group = json.loads(lease.read_text()).get('group') or group
                        except (OSError, ValueError): pass
                if parent.poll() is None: parent.kill(); parent.wait(timeout=5)
                if group:
                    try: slots.os.killpg(group, slots.signal.SIGKILL)
                    except ProcessLookupError: pass
                for fd in held: slots.os.close(fd)


if __name__ == "__main__": unittest.main()
