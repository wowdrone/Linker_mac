"""Wire compatibility is separate from Python module/profile compatibility."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import guide_protocol as linker
from test_guide_stage1 import MemoryNetwork, sentinel_namespace

sentinel = sentinel_namespace.load('guide_protocol')
FIXTURES = Path(__file__).parent/'fixtures'

class Compatibility(unittest.TestCase):
    def test_namespace_identity(self):
        import guide_protocol
        self.assertIs(guide_protocol, linker)
        self.assertIsNot(sentinel, linker)
        self.assertIs(sentinel_namespace.load('guide_sentinel').GuideLink, sentinel.GuideLink)
        self.assertIs(sentinel_namespace.load('obstacle_geo').checked_lat_lon, sentinel.checked_lat_lon)

    def test_profile_generations_and_extends_stage(self):
        old=json.loads((FIXTURES/'sentinel_legacy.json').read_text())
        current=json.loads((FIXTURES/'sentinel_current.json').read_text())
        with TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'old.json').write_text(json.dumps(old))
            self.assertEqual(linker.load_settings(root/'old.json')[1]['stage'],'STAGE1')
            with self.assertRaises(KeyError): sentinel.load_settings(root/'old.json')
            (root/'current.json').write_text(json.dumps(current))
            self.assertEqual(sentinel.load_settings(root/'current.json')[1]['stage'],'STAGE1')
            for stage in ('STAGE2','STAGE3'):
                (root/'child.json').write_text(json.dumps({'extends':'current.json','stage':stage}))
                loaded=sentinel.load_settings(root/'child.json')[1]
                self.assertEqual(loaded['stage'],stage)
                self.assertEqual(loaded['sentinel'],current['sentinel'])
                with self.assertRaises(ValueError): linker.load_settings(root/'child.json')
            (root/'loop.json').write_text('{"extends":"loop.json"}')
            with self.assertRaises(ValueError): sentinel.load_settings(root/'loop.json')

    def test_signed_old_current_unknown_health_and_direction(self):
        config=json.loads((FIXTURES/'sentinel_legacy.json').read_text())
        paths=[{'bind':('127.0.0.1',a),'peer':('127.0.0.1',b)} for a,b in ((20,21),(22,23))]
        logs=[]
        with patch('socket.socket',MemoryNetwork().socket):
            sender=sentinel.GuideLink(sentinel.SENTINEL,{**config,'paths':paths},b'a'*32,lambda *a,**k:None)
            receiver=linker.GuideLink(linker.LINKER,{**config,'paths':[{'bind':p['peer'],'peer':p['bind']} for p in paths]},b'a'*32,lambda *a,**k:logs.append(k))
            self.addCleanup(sender.close);self.addCleanup(receiver.close)
            for health in (1,3,4,64,127):
                packet=sender.make('GUIDE_HEARTBEAT',node_id=1,t_utc_ms=sentinel.utc_ms(),mode=0,
                    link_quality=1,health=health,snapshot_epoch=1,snapshot_count=0)
                sender.send(packet)
                received=list(receiver.receive())
                self.assertEqual(len(received),2)
                self.assertEqual(received[0][1].data['health'],health)
            packet=sender.make('GUIDE_HEARTBEAT',node_id=1,t_utc_ms=sentinel.utc_ms(),mode=0,
                link_quality=1,health=128,snapshot_epoch=1,snapshot_count=0)
            sender.send(packet);self.assertFalse(list(receiver.receive()))
            self.assertTrue(logs)

    def test_xml_and_generated_dialect_unchanged(self):
        root=Path(__file__).parents[1]
        self.assertEqual((root/'protocol/wow_guide.xml').read_bytes(),(sentinel_namespace.root/'protocol/wow_guide.xml').read_bytes())
        import wow_guide_mavlink as dialect
        self.assertEqual(set(dialect.mavlink_map),set(range(52200,52207)))

    def test_current_fc_peer_is_required_before_transport(self):
        current=json.loads((FIXTURES/'sentinel_current.json').read_text())
        current['course']=None
        current['sentinel'].pop('fc_peer',None)
        with TemporaryDirectory() as directory:
            path=Path(directory)/'current.json';path.write_text(json.dumps(current))
            with self.assertRaisesRegex(ValueError,'fc_peer is required'):
                sentinel_namespace.load('guide_sentinel').SentinelSession(path)

    def test_ttl_future_detection_and_expired_advice(self):
        config=json.loads((FIXTURES/'sentinel_legacy.json').read_text())
        now=linker.utc_ms()
        alert=dict(obstacle_id=1,type=1,confidence=.9,lat=37.,lon=127.,track_s_m=1.,
                   pos_err_m=.1,size_m=.1,lateral_m=0.,free_side=1,severity=2,state=0,
                   ttl_ms=100,t_detect_utc_ms=now,t_send_utc_ms=now)
        linker.validate('OBSTACLE_ALERT',alert,now,config)
        for changes in ({'ttl_ms':0},{'ttl_ms':config['hazard_ttl_ms']+1},
                        {'t_detect_utc_ms':now+1},{'t_detect_utc_ms':now-1000}):
            with self.assertRaises(ValueError):linker.validate('OBSTACLE_ALERT',{**alert,**changes},now,config)
        with self.assertRaises(ValueError):
            linker.validate('DRIVE_ADVICE',dict(advice_mode=1,target_speed_mps=1,
                lateral_offset_m=0,valid_until_ms=now-1,reason_obstacle_id=1),now,config)
