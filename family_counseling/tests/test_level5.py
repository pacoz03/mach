"""Local regression cases: run with the city-cli Python and unittest."""
import sys
from pathlib import Path
import unittest
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock, patch
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

    def test_alternative_requires_yes(self):
        history=[row('citizen:test','lang=en gender=m days=tue'),
                 row('agent:fc','data.request purpose=alternative lang=it gender=f days=wed')]
        self.assertEqual(f._family_preferences(history+[row('citizen:test','No')], 'citizen:test')['gender'],'m')
        p=f._family_preferences(history+[row('citizen:test','Si')], 'citizen:test')
        self.assertEqual(p,{'lang':'it','gender':'f','days':{'wed'}})


class Flows(unittest.IsolatedAsyncioTestCase):
    async def test_alternative_reply_never_books(self):
        for answer, accepted in [('Si',True),('No',False)]:
            city=Row(agent_id='agent:fc')
            m=row('citizen:test',answer)
            chat=[row('agent:fc','data.request purpose=alternative lang=it gender=f days=wed'),m]
            with patch.object(f,'_advance',new_callable=AsyncMock) as search, patch.object(f,'_say',new_callable=AsyncMock) as say, patch.object(f,'_on_confirm',new_callable=AsyncMock) as book:
                await f._family(city,m,chat)
                self.assertEqual(search.await_count,int(accepted)); book.assert_not_awaited()

    async def test_partner_combined_and_empty(self):
        for criteria, fits in [('lang=en gender_preference=f days=tue',True),
                               ('lang=en gender_preference=m days=tue',False),
                               ('lang=fr gender_preference=f days=tue',False),
                               ('lang=en gender_preference=f days=sun',False)]:
            sc=dict(start=('family_counseling',j.XREQ.format(svc='family_counseling.gynecology',proc='CONS-GIN',rid='L5')+' '+criteria), foreign=True)
            bus=j.Bus(sc,False); await bus.run(sc['start']); agent=bus.ids['family_counseling']
            replies=[m for m in bus.log if m.sender==agent and m.recipient==j.FOREIGN and 'request_id=L5' in m.text]
            self.assertEqual(len(replies),1)
            if fits:
                self.assertIn('agenda_day=tue',replies[0].text)
                self.assertTrue(any(j.act(m)=='booking.receipt' and m.recipient==j.FOREIGN for m in bus.log))
            else:
                self.assertIn('reason=no_availability',replies[0].text)
                self.assertFalse(any(m.sender==agent and m.recipient==j.LEDGER and j.act(m)=='hold' for m in bus.log))

if __name__=='__main__':unittest.main()
