"""Local regression cases: run with the city-cli Python and unittest."""
import sys
from pathlib import Path
import unittest
from types import SimpleNamespace as Row
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'agent'))
import facility as f
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'tests'))
import journey as j


def row(sender, text, body=None):
    return Row(sender=sender, recipient='agent:fc' if sender.startswith('citizen:') else 'citizen:test',
               text=text, body=body or {}, type='chat', need_id='N-test', msg_id=text)


class Preferences(unittest.TestCase):
    def test_combined_structured(self):
        p=f._family_preferences([row('citizen:test', '', {'provided': {'days':['tue'], 'lang':'en','gender_preference':'f'}})], 'citizen:test')
        self.assertEqual(p, {'days':{'tue'}, 'lang':'en', 'gender':'f'})


class Flows(unittest.IsolatedAsyncioTestCase):
    async def test_partner_combined_and_empty(self):
        for criteria, fits in [('lang=en gender_preference=f days=tue',True),
                               ('lang=en gender_preference=m days=tue',False),
                               ('lang=fr gender_preference=f days=tue',False),
                               ('lang=en gender_preference=f days=sun',False)]:
            sc=dict(start=('family_counseling',j.XREQ.format(svc='family_counseling.gynecology',proc='CONS-GIN',rid='L5')+' '+criteria), foreign=True)
            bus=j.Bus(sc,False); await bus.run(sc['start']); agent=bus.ids['family_counseling']
            replies=[m for m in bus.log if m.sender==agent and m.recipient==j.FOREIGN and 'request_id=L5' in m.text and j.act(m) in {'offer','cancel'}]
            self.assertEqual(len(replies),1)
            if fits:
                self.assertIn('agenda_day=tue',replies[0].text)
                self.assertTrue(any(j.act(m)=='booking.receipt' and m.recipient==j.FOREIGN for m in bus.log))
            else:
                self.assertIn('reason=no_availability',replies[0].text)
                self.assertFalse(any(m.sender==agent and m.recipient==j.LEDGER and j.act(m)=='hold' for m in bus.log))

if __name__=='__main__':unittest.main()
