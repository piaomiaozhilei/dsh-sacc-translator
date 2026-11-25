import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'runtime'))
from ocr_runtime import all_cuda, warmup_cuda
from types import SimpleNamespace

class RuntimeTests(unittest.TestCase):
    def test_every_model_must_be_cuda(self):
        self.assertTrue(all_cuda({k:['CUDAExecutionProvider','CPUExecutionProvider'] for k in ('det','cls','rec')}))
        self.assertFalse(all_cuda({'det':['CUDAExecutionProvider']}))
        self.assertFalse(all_cuda({'det':['CUDAExecutionProvider'],'cls':['CPUExecutionProvider'],'rec':['CUDAExecutionProvider']}))

    def test_warmup_executes_all_models_without_silent_fallback(self):
        calls=[]
        class Session:
            def disable_fallback(self): calls.append('disable')
            def get_inputs(self): return [SimpleNamespace(name='x',shape=[None,3,None,None])]
            def run(self,_,feed): calls.append(feed['x'].shape)
        engine=SimpleNamespace(**{name:SimpleNamespace(session=SimpleNamespace(session=Session())) for name in ('text_det','text_cls','text_rec')})
        warmup_cuda(engine)
        self.assertEqual(calls,['disable',(1,3,320,320),'disable',(1,3,48,192),'disable',(1,3,48,320)])

if __name__=='__main__': unittest.main()
