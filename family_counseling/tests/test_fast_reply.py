"""Fast-reply regression: cache must answer before any slow Ledger write."""
import sys
from pathlib import Path
import asyncio
import time
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace as Row
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'agent'))
import facility as f

class FastReply(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        f._WORLD.clear()
        self.now=datetime(2026,11,23,8,tzinfo=timezone.utc)
        self.world=('slots page=1/1 count=1\nslot_id=S-fast doctor_id=asst-gorla-c1 '
                    'facility=agent:fc procedure=CONS-GIN at=2026-11-24T15:00:00+00:00 day=tue free=1')
        self.city=Row(agent_id='agent:fc',history=AsyncMock(return_value=[]))

    def message(self,rid,criteria='lang=en gender_preference=f days=tue'):
        text=f'need.request service_type=family_counseling.gynecology procedure=CONS-GIN priority=B request_id={rid} {criteria}'
        return Row(sender='agent:partner',recipient='agent:fc',need_id='N-fast',msg_id=rid,text=text,body={'text':text},type='chat')

    async def test_warm_cache_answers_before_12_second_ledger(self):
        f._WORLD[('CONS-GIN',1)]=(time.monotonic(),self.world)
        messages=[]; started=time.monotonic()
        async def say(city,to,text):
            messages.append((to,text,time.monotonic()-started))
            if to==f.LEDGER:
                # Abort slow refresh once the first ledger call is reached. If cache
                # works, the useful partner response must already have been emitted.
                raise asyncio.CancelledError()
            return True
        with patch.object(f,'_city_now',return_value=self.now),patch.object(f,'_say',side_effect=say):
            with self.assertRaises(asyncio.CancelledError):
                await f._cross_asst(self.city,self.message('warm-fast'),procedures=('CONS-GIN',))
        self.assertEqual(messages[0][0],'agent:partner')
        self.assertIn('offer offer_id=',messages[0][1]);self.assertLess(messages[0][2],10)
        self.assertIn('agenda_day=tue',messages[0][1])

    async def test_warm_cache_keeps_constraints(self):
        f._WORLD[('CONS-GIN',1)]=(time.monotonic(),self.world)
        say=AsyncMock(return_value=True)
        with patch.object(f,'_city_now',return_value=self.now),patch.object(f,'_say',say),patch.object(f,'_until',new_callable=AsyncMock,return_value=[]):
            await f._cross_asst(self.city,self.message('wrong-gender','gender_preference=m'),procedures=('CONS-GIN',))
        self.assertIn('cancel reason=no_availability',say.await_args_list[0].args[2])

    async def test_cold_timeout_never_claims_no_slots(self):
        say=AsyncMock(return_value=True)
        with patch.object(f,'_city_now',return_value=None),patch.object(f,'_say',say),patch.object(f,'_until',new_callable=AsyncMock,return_value=[]):
            await f._cross_asst(self.city,self.message('cold-fast'),procedures=('CONS-GIN',))
        replies=[call.args[2] for call in say.await_args_list if call.args[1]=='agent:partner']
        self.assertEqual(len(replies),1)
        self.assertNotIn('no_availability',replies[0]);self.assertNotIn('offer offer_id=',replies[0])

    async def test_real_slow_history_and_ledger(self):
        f._WORLD[("CONS-GIN",1)]=(time.monotonic(),self.world)
        replied=asyncio.Event(); messages=[]
        async def history(**kwargs):
            await asyncio.sleep(12)
            return []
        async def say(city,to,text):
            if to==f.LEDGER:
                await asyncio.sleep(12)
            else:
                messages.append(text); replied.set()
            return True
        self.city.history=history
        start=time.monotonic()
        with patch.object(f,"_city_now",return_value=self.now),patch.object(f,"_say",side_effect=say):
            task=asyncio.create_task(f._cross_asst(self.city,self.message("real-slow"),procedures=("CONS-GIN",)))
            try:
                await asyncio.wait_for(replied.wait(),timeout=3)
                self.assertLess(time.monotonic()-start,3)
                self.assertIn("offer offer_id=",messages[0])
            finally:
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass

    async def test_pending_ack_does_not_block_redelivery(self):
        f._WORLD[("CONS-GIN",1)]=(time.monotonic(),self.world)
        pending=Row(sender="agent:fc",recipient="agent:partner",text="request_id=retry-pending Ricevuto",msg_id="ack")
        self.city.history=AsyncMock(return_value=[pending])
        say=AsyncMock(return_value=True)
        with patch.object(f,"_city_now",return_value=self.now),patch.object(f,"_say",say),patch.object(f,"_until",new_callable=AsyncMock,return_value=[]):
            await f._cross_asst(self.city,self.message("retry-pending"),procedures=("CONS-GIN",))
        self.assertTrue(any("offer offer_id=" in c.args[2] for c in say.await_args_list))

if __name__=='__main__':unittest.main()
