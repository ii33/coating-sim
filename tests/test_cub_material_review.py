"""Material business regressions: shared-process locking and existing filenames."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import multiprocessing
import os
from pathlib import Path
import queue
import signal
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from coat_sim.core.material_business import MaterialLibrary
from coat_sim.core.materials import (default_material_profile, delete_material,
                                     material_library_lock, save_material)


def _create_in_process(root, identifier, mode, messages):
    messages.put(("ready", identifier))
    try:
        record = default_material_profile(identifier, identifier)
        if mode == "business":
            MaterialLibrary(root).write(record)
        else:
            save_material(Path(root) / "data/materials", record)
        messages.put(("created", identifier))
    except Exception as exc:
        messages.put(("error", identifier, type(exc).__name__, str(exc)))


def _connected_material_mcp(method):
    async def wrapped(self):
        # AnyIO requires entering/exiting a cancel scope in the same task;
        # unittest runs setup, the test, and teardown in separate tasks.
        async with self.context as client:
            self.client = client
            await method(self)
    wrapped.__name__ = method.__name__
    return wrapped


def _fork_with_abandoned_parent_lock(directory, connection):
    with material_library_lock(directory):
        child = os.fork()
        if child:
            connection.send(("child", child))
            # Deliberately bypass context-manager unlock. The child's inherited
            # descriptor must be closed, so this exit frees the parent's lock.
            os._exit(0)
        try:
            with material_library_lock(directory):
                connection.send(("acquired", os.getpid()))
        finally:
            os._exit(0)


def _fork_and_unwind_inherited_lock(directory, connection):
    child = None
    try:
        with material_library_lock(directory):
            child = os.fork()
            if child:
                connection.send(("child", child))
                os.waitpid(child, 0)
        if child == 0:
            # Exiting the inherited context must not unlock or double-close
            # the descriptor already closed by the child fork hook.
            connection.send(("unwound", os.getpid()))
    except Exception as exc:
        connection.send(("error", type(exc).__name__, str(exc)))
    finally:
        if child == 0:
            os._exit(0)


class CubMaterialReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.lib = MaterialLibrary(self.root)

    def _custom(self):
        return [row for row in self.lib.list(limit=100)["items"] if not row["readonly"]]

    def test_casefold_create_race_across_threads(self):
        start = threading.Barrier(2)
        def create(identifier):
            record = default_material_profile(identifier, identifier)
            start.wait(timeout=5)
            try:
                self.lib.write(record)
                return "created"
            except ValueError:
                return "conflict"
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(create, ("CaseId", "caseid")))
        self.assertCountEqual(results, ["created", "conflict"])
        self.assertEqual(len(self._custom()), 1)
        self.assertFalse(list(self.lib.directory.glob("*.tmp")))

    def test_gui_core_and_api_share_cross_process_lock(self):
        for method in ("spawn", "fork"):
            if method in multiprocessing.get_all_start_methods():
                with self.subTest(method=method):
                    self._check_cross_process_lock(method)

    def _check_cross_process_lock(self, method):
        root = self.root / method
        lib = MaterialLibrary(root)
        context = multiprocessing.get_context(method)
        messages = context.Queue()
        processes = [context.Process(target=_create_in_process,
                        args=(str(root), identifier, mode, messages))
                     for identifier, mode in (("MixedId", "business"), ("mixedid", "core"))]
        try:
            with material_library_lock(lib.directory):
                for process in processes:
                    process.start()
                ready = [messages.get(timeout=10), messages.get(timeout=10)]
                self.assertTrue(all(row[0] == "ready" for row in ready), ready)
                with self.assertRaises(queue.Empty):
                    messages.get(timeout=.25)
                self.assertTrue(all(process.is_alive() for process in processes))
            results = [messages.get(timeout=10), messages.get(timeout=10)]
            self.assertCountEqual([row[0] for row in results], ["created", "error"])
            self.assertEqual(len([row for row in lib.list(limit=100)["items"] if not row["readonly"]]), 1)
        finally:
            for process in processes:
                process.join(timeout=10)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=3)
            messages.close()
        self.assertTrue(all(process.exitcode == 0 for process in processes))

    def test_shared_lock_is_reentrant_for_api_save_and_delete(self):
        record = default_material_profile("one", "one")
        with material_library_lock(self.lib.directory):
            self.lib.write(record)
            record["name"] = "updated"
            self.lib.write(record, update=True)
            delete_material(self.lib.directory, "one")
        self.assertEqual(self._custom(), [])

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX inherited descriptor regression")
    def test_fork_child_drops_lock_descriptor_when_parent_exits(self):
        self._check_fork_lifetime(_fork_with_abandoned_parent_lock, "acquired")

    @unittest.skipUnless(hasattr(os, "fork"), "POSIX inherited context regression")
    def test_fork_child_can_unwind_inherited_lock(self):
        self._check_fork_lifetime(_fork_and_unwind_inherited_lock, "unwound")

    def _check_fork_lifetime(self, target, expected):
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(target=target,
                                  args=(str(self.lib.directory), sender))
        child = None
        try:
            process.start()
            self.assertTrue(receiver.poll(10), "parent did not report forked child")
            marker, child = receiver.recv()
            self.assertEqual(marker, "child")
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
            self.assertTrue(receiver.poll(10), "child retained abandoned parent lock")
            self.assertEqual(receiver.recv(), (expected, child))
            child = None  # The grandchild exits immediately after this message.
        finally:
            if child is not None:
                try:
                    os.kill(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)
            receiver.close()
            sender.close()

    def test_renamed_record_updates_its_existing_file(self):
        self.lib.write(default_material_profile("original", "custom_id"))
        original = self.lib.directory / "custom_id.json"
        renamed = self.lib.directory / "renamed.json"
        original.rename(renamed)
        record = self.lib.read("custom_id")["record"]
        record["name"] = "updated"
        result = self.lib.write(record, update=True)
        self.assertFalse(result["created"])
        self.assertFalse(original.exists())
        self.assertEqual(json.loads(renamed.read_text())["name"], "updated")
        self.assertEqual(len(self._custom()), 1)
        self.assertEqual(self.lib.read("custom_id")["record"]["name"], "updated")

    def test_renamed_update_publication_failure_preserves_bytes(self):
        self.lib.write(default_material_profile("original", "custom_id"))
        renamed = self.lib.directory / "renamed.json"
        (self.lib.directory / "custom_id.json").rename(renamed)
        before = renamed.read_bytes()
        record = self.lib.read("custom_id")["record"]
        record["name"] = "failed update"
        original_close = os.close
        with patch("coat_sim.core.materials.os.replace", side_effect=OSError("injected")), \
                patch("coat_sim.core.materials.os.close", wraps=original_close) as close:
            with self.assertRaises(OSError):
                self.lib.write(record, update=True)
        closed = [call.args[0] for call in close.call_args_list]
        self.assertTrue(closed)
        for descriptor in closed:
            with self.assertRaises(OSError):
                os.fstat(descriptor)
        self.assertEqual(renamed.read_bytes(), before)
        self.assertFalse((self.lib.directory / "custom_id.json").exists())
        self.assertFalse(list(self.lib.directory.glob("*.tmp")))
        self.assertEqual(len(self._custom()), 1)

    def test_conflicting_case_updates_and_existing_collision_rejected(self):
        record = default_material_profile("one", "CaseId")
        self.lib.write(record)
        wrong_case = deepcopy(record)
        wrong_case["id"] = "caseid"
        with self.assertRaises(ValueError):
            self.lib.write(wrong_case, update=True)
        with self.assertRaises(ValueError):
            save_material(self.lib.directory, wrong_case, overwrite=True)
        # Existing imported libraries with ambiguous casefold identities must
        # fail closed rather than silently select one record for replacement.
        # Windows filenames are case-insensitive: use a genuinely distinct
        # filename while preserving the conflicting casefold record ID.
        (self.lib.directory / "conflicting-import.json").write_text(json.dumps(wrong_case))
        with self.assertRaisesRegex(ValueError, "duplicate material id ignoring case"):
            self.lib.list()

    def test_builtin_copy_preserves_roles_optics_and_provenance(self):
        identifier = self.lib.list(limit=1)["items"][0]["id"]
        original = self.lib.read(identifier)["record"]
        with self.assertRaisesRegex(ValueError, "builtin_override_required"):
            self.lib.write(original, update=True)
        copied = self.lib.copy(identifier, "custom_copy", "copied")["record"]
        expected = deepcopy(original)
        expected.update(id="custom_copy", name="copied")
        self.assertEqual(copied, expected)
        self.assertEqual(self.lib.read(identifier)["record"], original)

    def test_stale_complete_record_remains_explicit_last_writer_wins(self):
        self.lib.write(default_material_profile("original", "record"))
        first = self.lib.read("record")["record"]
        stale = self.lib.read("record")["record"]
        first["name"] = "first change"
        self.lib.write(first, update=True)
        stale["notes"] = "second change"
        self.lib.write(stale, update=True)
        saved = self.lib.read("record")["record"]
        self.assertEqual(saved["name"], "original")
        self.assertEqual(saved["notes"], "second change")

    def test_linked_data_and_lock_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        try:
            (self.root / "data").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(str(exc))
        with self.assertRaises(ValueError):
            self.lib.write(default_material_profile("one", "one"))
        self.assertEqual(list(outside.iterdir()), [])
        (self.root / "data").unlink()
        self.lib.directory.mkdir(parents=True)
        target = outside / "target"
        target.write_text("unchanged")
        (self.lib.directory / ".material-library.lock").symlink_to(target)
        with self.assertRaises(ValueError):
            self.lib.write(default_material_profile("one", "one"))
        self.assertEqual(target.read_text(), "unchanged")
        self.assertFalse(list(self.lib.directory.glob("*.json")))

    def test_reparse_lock_rejected_before_open(self):
        self.lib.directory.mkdir(parents=True)
        lock = self.lib.directory / ".material-library.lock"
        lock.write_text("unchanged")
        original_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path == lock:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with patch.object(Path, "lstat", lstat):
            with self.assertRaisesRegex(ValueError, "锁必须是普通文件"):
                self.lib.write(default_material_profile("one", "one"))
        self.assertEqual(lock.read_text(), "unchanged")

    @unittest.skipIf(os.name == "nt", "POSIX acquisition failure injection")
    def test_lock_acquisition_failure_closes_descriptor(self):
        original_close = os.close
        with patch("fcntl.flock", side_effect=OSError("injected acquisition failure")), \
                patch("coat_sim.core.materials.os.close", wraps=original_close) as close:
            with self.assertRaisesRegex(OSError, "acquisition failure"):
                with material_library_lock(self.lib.directory):
                    self.fail("lock should not have been acquired")
        close.assert_called_once()
        descriptor = close.call_args.args[0]
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        # Failed acquisition must not leave a reentrant-held marker behind.
        self.lib.write(default_material_profile("one", "one"))


class CubMaterialMCPReviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from mcp import Client
        from coat_sim.core.api import CoatService
        from coat_sim.mcp import server
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = CoatService(root=self.tmp.name)
        service_patch = patch.object(server, "service", self.service)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.context = Client(server.mcp)

    @_connected_material_mcp
    async def test_pagination_rejects_mcp_boolean_and_string_coercion(self):
        for arguments in ({"offset": True}, {"offset": "0"}, {"limit": True}, {"limit": "1"}):
            with self.subTest(arguments=arguments):
                result = await self.client.call_tool("list_material_library", arguments)
                self.assertTrue(result.is_error, str(result.content))
        result = await self.client.call_tool("list_material_library", {"offset": 0, "limit": 1})
        self.assertFalse(result.is_error, str(result.content))

    @_connected_material_mcp
    async def test_optical_query_rejects_invalid_empty_and_nonempty_sets(self):
        from coat_sim.agent.tool_dispatch import result_value
        dataset = {"id": "test", "name": "test", "kind": "tabulated",
                   "wavelength_unit": "um", "wavelength_min": .5, "wavelength_max": 1.5,
                   "wavelengths": [.5, 1.5], "n": [1.5, 1.6], "k": None}
        for datasets in ([], [dataset]):
            for wavelength in (True, "1", -1, 0, float("inf"), float("nan")):
                with self.subTest(datasets=bool(datasets), wavelength=wavelength):
                    result = await self.client.call_tool("validate_material_optics", {
                        "datasets": datasets, "wavelength": wavelength, "wavelength_unit": "um"})
                    self.assertTrue(result.is_error, str(result.content))
            result = await self.client.call_tool("validate_material_optics", {
                "datasets": datasets, "wavelength": 1, "wavelength_unit": "um"})
            self.assertFalse(result.is_error, str(result.content))
            value = result_value(result)
            self.assertTrue(value["valid"])
            if datasets:
                self.assertEqual(value["evaluations"][0]["n"], 1.55)
                self.assertIsNone(value["evaluations"][0]["k"])
            else:
                self.assertEqual(value["evaluations"], [])

    async def test_optical_empty_set_api_rejects_invalid_units(self):
        with self.assertRaises(ValueError):
            self.service.validate_material_optics([], wavelength=1, wavelength_unit="angstrom")


if __name__ == "__main__":
    unittest.main()
