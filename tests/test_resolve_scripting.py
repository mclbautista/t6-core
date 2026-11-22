"""The Resolve scripting adapter against a fake Resolve app (no Resolve in CI)."""
import unittest
from typing import Any

from t6.resolve.timelines import ResolveTimeline, ScriptingTimelines, StampFailure

REF = 'db1/Shows/show'
STAMP = 't6t_' + 'a' * 32
OTHER = 't6t_' + 'b' * 32


class FakeTimeline:
    def __init__(self, uuid: str, name: str, stamp: str | None = None) -> None:
        self.uuid, self.name = uuid, name
        self.markers: dict[int, dict[str, Any]] = {} if stamp is None else {0: {'customData': stamp}}
        self.drop_writes = False

    def GetUniqueId(self) -> str:
        return self.uuid

    def GetName(self) -> str:
        return self.name

    def GetMarkers(self) -> dict[int, dict[str, Any]]:
        return dict(self.markers)

    def GetMarkerCustomData(self, frame: int) -> str:
        return str(self.markers.get(frame, {}).get('customData', ''))

    def AddMarker(self, frame: int, color: str, name: str, note: str, duration: int, custom: str) -> bool:
        if frame in self.markers:
            return False
        self.markers[frame] = {'customData': '' if self.drop_writes else custom}
        return True

    def UpdateMarkerCustomData(self, frame: int, custom: str) -> bool:
        if frame not in self.markers:
            return False
        self.markers[frame]['customData'] = '' if self.drop_writes else custom
        return True


class FakeProject:
    def __init__(self, timelines: list[FakeTimeline]) -> None:
        self.timelines = timelines

    def GetTimelineCount(self) -> int:
        return len(self.timelines)

    def GetTimelineByIndex(self, i: int) -> FakeTimeline:
        return self.timelines[i - 1]


class FakeManager:
    def __init__(self, projects: dict[str, FakeProject]) -> None:
        self.projects, self.locked, self.saves = projects, False, 0
        self.db, self.path = 'db1', ''

    def GetCurrentDatabase(self) -> dict[str, str]:
        return {'DbType': 'PostgreSQL', 'DbName': self.db}

    def GetDatabaseList(self) -> list[dict[str, str]]:
        return [{'DbType': 'PostgreSQL', 'DbName': n} for n in ('db1', 'db2')]

    def SetCurrentDatabase(self, db: dict[str, str]) -> bool:
        self.db = db['DbName']
        return True

    def GotoRootFolder(self) -> bool:
        self.path = self.db
        return True

    def OpenFolder(self, name: str) -> bool:
        if not any(ref.startswith(f'{self.path}/{name}/') for ref in self.projects):
            return False
        self.path = f'{self.path}/{name}'
        return True

    def GetProjectListInCurrentFolder(self) -> list[str]:
        return [ref.rsplit('/', 1)[1] for ref in self.projects if ref.rsplit('/', 1)[0] == self.path]

    def LoadProject(self, name: str) -> FakeProject | None:
        return None if self.locked else self.projects[f'{self.path}/{name}']

    def SaveProject(self) -> bool:
        self.saves += 1
        return True


class FakeResolve:
    def __init__(self, manager: FakeManager) -> None:
        self.manager = manager

    def GetProjectManager(self) -> FakeManager:
        return self.manager


class ScriptingTimelinesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.ep1 = FakeTimeline('u1', 'Ep1', STAMP)
        self.ep2 = FakeTimeline('u2', 'Ep2')
        self.manager = FakeManager({REF: FakeProject([self.ep1, self.ep2])})
        self.adapter = ScriptingTimelines(lambda: FakeResolve(self.manager))

    def test_reads_name_uuid_and_first_frame_stamp(self) -> None:
        self.ep2.markers[48] = {'customData': OTHER}  # not on the first frame
        self.assertEqual(self.adapter.timelines(REF), (ResolveTimeline(uuid='u1', name='Ep1', stamp=STAMP), ResolveTimeline(uuid='u2', name='Ep2', stamp=None)))

    def test_switches_to_the_project_database(self) -> None:
        self.manager.projects['db2/show'] = FakeProject([])
        self.assertEqual(self.adapter.timelines('db2/show'), ())

    def test_project_that_cannot_be_loaded_gives_a_reason(self) -> None:
        self.assertEqual(ScriptingTimelines(lambda: None).read(REF), StampFailure('Resolve is not running'))
        self.assertIsNone(ScriptingTimelines(lambda: None).timelines(REF))
        self.assertEqual(self.adapter.read('db1/Shows/missing'), StampFailure('The Resolve project is missing'))
        self.assertEqual(self.adapter.read('db1/Nope/show'), StampFailure('The Resolve project folder is missing'))
        self.assertEqual(self.adapter.read('db9/Shows/show'), StampFailure("The Resolve database 'db9' is missing or cannot be opened"))
        self.manager.locked = True
        self.assertEqual(self.adapter.stamp(REF, 'Ep2', OTHER), StampFailure('The Resolve project cannot be loaded (locked or open elsewhere)'))

    def test_writes_the_stamp_on_the_first_frame_and_saves(self) -> None:
        self.assertEqual(self.adapter.stamp(REF, 'u2', OTHER), ResolveTimeline(uuid='u2', name='Ep2', stamp=OTHER))
        self.assertEqual(self.ep2.markers[0]['customData'], OTHER)
        self.assertEqual(self.manager.saves, 1)

    def test_same_stamp_again_writes_and_saves_nothing(self) -> None:
        self.assertEqual(self.adapter.stamp(REF, 'Ep1', STAMP), ResolveTimeline(uuid='u1', name='Ep1', stamp=STAMP))
        self.assertEqual(self.manager.saves, 0)

    def test_refusals_write_nothing(self) -> None:
        self.ep2.markers[0] = {'customData': ''}
        cases = {
            ('Ep1', OTHER): 'The timeline carries a different stamp',
            ('Ep2', STAMP): 'The stamp is already on another timeline of the Resolve project',
            ('Ep2', OTHER): 'The first frame has a marker without a stamp; T6 does not change it',
            ('Ep3', OTHER): 'No timeline of that name or Resolve ID in the Resolve project',
        }
        for (name, stamp), reason in cases.items():
            self.assertEqual(self.adapter.stamp(REF, name, stamp), StampFailure(reason))
        self.assertEqual((self.ep1.markers, self.ep2.markers), ({0: {'customData': STAMP}}, {0: {'customData': ''}}))
        self.assertEqual(self.manager.saves, 0)

    def test_two_timelines_of_one_name_is_refused(self) -> None:
        self.ep2.name = 'Ep1'
        self.assertEqual(self.adapter.stamp(REF, 'Ep1', OTHER), StampFailure('More than one timeline of that name; give its Resolve ID'))

    def test_restamps_a_duplicate_by_updating_its_marker(self) -> None:
        copy = FakeTimeline('u9', 'Ep1 copy', STAMP)
        self.manager.projects[REF].timelines.append(copy)
        self.assertEqual(self.adapter.stamp(REF, 'u9', OTHER, replaces=STAMP), ResolveTimeline(uuid='u9', name='Ep1 copy', stamp=OTHER))
        self.assertEqual(self.ep1.markers[0]['customData'], STAMP)
        self.assertEqual(self.manager.saves, 1)

    def test_restamp_of_a_timeline_that_is_not_a_duplicate_is_refused(self) -> None:
        self.assertEqual(self.adapter.stamp(REF, 'Ep1', OTHER, replaces=STAMP), StampFailure('Only a duplicate is re-stamped; no other timeline carries that stamp'))
        self.assertEqual(self.ep1.markers[0]['customData'], STAMP)

    def test_read_back_mismatch_is_a_failure_and_not_saved(self) -> None:
        self.ep2.drop_writes = True
        self.assertEqual(self.adapter.stamp(REF, 'Ep2', OTHER), StampFailure("Read-back mismatch: the first-frame marker holds ''"))
        self.assertEqual(self.manager.saves, 0)


if __name__ == '__main__':
    unittest.main()
