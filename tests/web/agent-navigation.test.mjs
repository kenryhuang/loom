import assert from 'node:assert/strict';
import {spawn} from 'node:child_process';
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {JSDOM} from 'jsdom';

async function until(predicate) {
  const deadline=Date.now()+10000;
  while(!predicate()) {
    if(Date.now()>deadline)throw new Error('Timed out waiting for navigation');
    await new Promise(resolve=>setTimeout(resolve,20));
  }
}

test('Agent deep link does not request Sessions until navigating to Sessions',async()=>{
  const root=fileURLToPath(new URL('../../',import.meta.url));
  const child=spawn(process.env.LOOM_TEST_PYTHON || `${root}.venv/bin/python`,['tests/web/smoke.py'],{
    cwd:root,env:{...process.env,PYTHONPATH:'src:.'},stdio:['ignore','pipe','pipe'],
  });
  const exited=new Promise(resolve=>child.once('exit',resolve));
  let output='',stderr='',dom;
  child.stdout.on('data',data=>output+=data);child.stderr.on('data',data=>stderr+=data);
  const originalFetch=globalThis.fetch,requests=[];
  try {
    await until(()=>output.includes('Credential:'));
    const url=/http:\/\/127\.0\.0\.1:\d+\/web\//.exec(output)[0];
    dom=new JSDOM(await readFile(new URL('../../src/loom/web/assets/index.html',import.meta.url),'utf8'),{
      url:`${url}#/agents`,pretendToBeVisual:true,
    });
    for(const key of ['window','document','history','location'])globalThis[key]=dom.window[key];
    dom.window.HTMLDialogElement.prototype.showModal=function(){this.open=true;};
    dom.window.HTMLDialogElement.prototype.close=function(){this.open=false;};
    globalThis.fetch=(path,options)=>{
      const target=new URL(path,url);requests.push(target.pathname);
      return originalFetch(target,options);
    };
    await import('../../src/loom/web/assets/app.mjs');
    await until(()=>document.getElementById('auth-dialog').open);
    document.getElementById('credential').value='browser-smoke-token';
    document.getElementById('auth-form').dispatchEvent(new dom.window.Event('submit',{bubbles:true,cancelable:true}));
    await until(()=>document.getElementById('agents-list').textContent.includes('Create Agent'));
    assert.equal(requests.includes('/v1/sessions'),false);
    assert.equal(document.getElementById('agents-workspace').hidden,false);
    document.querySelector('[data-component="sessions"]').click();
    await until(()=>document.querySelector('#session-list .session-link'));
    assert.equal(requests.filter(path=>path==='/v1/sessions').length,1);
    assert.match(location.hash,/^#\/sessions\//);
  } catch(error) {
    error.message+=`\n${stderr}`;throw error;
  } finally {
    dom?.window.dispatchEvent(new dom.window.Event('pagehide'));
    dom?.window.close();globalThis.fetch=originalFetch;
    child.kill('SIGINT');await exited;
  }
});
