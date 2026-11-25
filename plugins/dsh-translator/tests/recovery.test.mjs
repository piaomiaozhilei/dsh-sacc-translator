import test from 'node:test';
import assert from 'node:assert/strict';
import {recoverTranslations, TranslationFormatError, decodeSingleTranslation, singleTranslationPrompt} from '../src/prompt.js';

const units = [{id:'stable-a',text:'Hello'}, {id:'stable-b',text:'World'}];
test('missing batch items fall back individually without retranslating successes', async () => {
  const calls = [];
  const result = await recoverTranslations(units, async (subset, single) => {
    calls.push([subset.map(x=>x.id), single]);
    return single ? decodeSingleTranslation('"世界"', subset[0]) : {'stable-a':'你好'};
  }, new AbortController().signal);
  assert.deepEqual(result, {'stable-a':'你好','stable-b':'世界'});
  assert.deepEqual(calls, [[['stable-a','stable-b'],false],[['stable-b'],true]]);
});
test('malformed batch falls back to bounded single requests; failures preserve valid items', async () => {
  let calls = 0;
  const result = await recoverTranslations(units, async (subset, single) => {
    calls++;
    if (!single || subset[0].id === 'stable-b') throw new TranslationFormatError('invalid');
    return {'stable-a':'你好'};
  }, new AbortController().signal);
  assert.equal(calls,3);
  assert.deepEqual(result,{'stable-a':'你好'});
});
test('network errors and cancellation do not trigger model retries', async () => {
  let calls = 0;
  await assert.rejects(recoverTranslations(units, async () => {calls++;throw new Error('offline');}, new AbortController().signal), /offline/);
  assert.equal(calls,1);
  const controller = new AbortController();
  await assert.rejects(recoverTranslations(units, async () => {controller.abort();return {'stable-a':'你好'};}, controller.signal), {name:'AbortError'});
});
test('singleton format binds local ID, rejects unstructured text and ambiguous output', () => {
  assert.deepEqual(decodeSingleTranslation('```json\n"你好"\n```',units[0]),{'stable-a':'你好'});
  assert.deepEqual(decodeSingleTranslation('{"0":"你好"}',units[0]),{'stable-a':'你好'});
  for (const raw of ['你好','""','null','["你好"]','"截断','{"1":"错位"}']) assert.throws(()=>decodeSingleTranslation(raw,units[0]));
  const prompt = singleTranslationPrompt('auto','zh',units[0]);
  assert.equal(JSON.parse(prompt.input),'Hello');
  assert.ok(!prompt.input.includes('stable-a'));
});
