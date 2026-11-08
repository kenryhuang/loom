import {element} from '../markdown.mjs';
import {field, selectField, preset} from './agent-setup.mjs';
import {agentResourceChoices} from './agent-resources.mjs';
import {selectedTools} from './external-tools.mjs';

export const configurationTitles = {identity:'Role & methods', models:'Models', execution:'Execution',
  scheduling:'Scheduling', budget:'Daily budget', learning:'Learning & evaluation', resources:'Sources & resources'};
export const configuredRole = detail => detail.configuration?.role || detail.role;
const lines = value => value.split('\n').map(row=>row.trim()).filter(Boolean);
const intervals = [['60','1 minute'],['300','5 minutes'],['900','15 minutes'],['3600','1 hour'],['86400','Daily']];
const budgets = [['250000','250k tokens'],['1000000','1M tokens'],['10000000','10M tokens'],['100000000','100M tokens']];
const action = (label, callback) => {
  const button=element('button','quiet',label);button.type='button';button.onclick=callback;return button;
};
function choices(select, rows, configured) {
  select.replaceChildren();
  for(const [value,label] of [['','Service default'],...rows]) {
    const option=element('option','',label);option.value=value;select.append(option);
  }
  if(configured && ![...select.options].some(option=>option.value===configured)) {
    const missing=element('option','',`${configured} (currently unavailable)`);missing.value=configured;select.append(missing);
  }
  select.value=configured || '';
}

export class AgentConfigurationEditor {
  constructor(root,{detail,section,api,save,cancel}) {
    this.root=root;this.detail=detail;this.section=section;this.api=api;this.save=save;this.cancel=cancel;
    this.role=structuredClone(configuredRole(detail));this.abort=new AbortController();this.closed=false;
    const form=this.form=element('form','agent-form agent-configuration-form');form.dataset.configurationEditor=section;
    form.setAttribute('aria-label',`Edit ${configurationTitles[section]}`);
    form.append(element('h4','',`Edit ${configurationTitles[section]}`),
      element('p','muted small','Saved settings apply to the next newly scheduled task; dispatched tasks and their recovery keep the original configuration.'));
    this.fields=element('div','setup-settings-grid');form.append(this.fields);
    this.build();
    this.error=element('p','error');this.error.setAttribute('role','alert');
    this.retry=action('Reload choices',()=>this.load());this.retry.hidden=true;
    this.submit=element('button','primary','Save settings');this.submit.type='submit';
    this.cancelButton=action('Cancel',()=>{this.close();this.cancel();});
    const actions=element('div','setup-form-actions');actions.append(this.submit,this.cancelButton);
    form.append(this.error,this.retry,actions);root.append(form);
    form.onsubmit=async event=>{
      event.preventDefault();if(this.submit.disabled || !form.reportValidity())return;
      this.submit.disabled=true;this.cancelButton.disabled=true;this.error.textContent='';
      try{await this.save(this.changes());}
      catch(error){if(!this.closed)this.error.textContent=`${error.message}${error.status===409?' Cancel and reopen to check the latest configuration.':''}`;}
      finally{if(!this.closed){this.submit.disabled=false;this.cancelButton.disabled=false;}}
    };
  }
  close(){this.closed=true;this.abort.abort();this.form.remove();}
  build(){
    const p=this.fields,r=this.role;
    if(this.section==='identity') {
      field(p,'name','Name',r.name,{required:true});field(p,'role_text','Role description',r.role_text,{required:true,multiline:true});
      field(p,'methods','Default methods (one per line)',(r.methods || []).join('\n'),{multiline:true});
    } else if(this.section==='models') {
      this.model=selectField(p,'model','Execution & planning model',[['','Loading models…']]);
      this.evaluation=selectField(p,'evaluation_model','Evaluation model',[['','Loading models…']]);
    } else if(this.section==='budget') {
      this.daily=preset(p,'daily_tokens','Daily token limit',budgets,r.budget?.daily_tokens || 1000000);
      p.append(element('p','muted small',`Planning, execution, acceptance, and learning share the daily allowance, resetting at midnight ${r.timezone || 'UTC'}.`));
    } else if(this.section==='scheduling') {
      const s=r.scheduling_policy || {};
      this.interval=preset(p,'interval_seconds','Goal check interval (seconds)',intervals,r.interval_seconds || 86400,{min:10,max:31536000});
      this.enabled=selectField(p,'enabled','Scheduled runs',[['true','On'],['false','Manual only']],String(s.enabled ?? true));
      this.tick=preset(p,'tick_seconds','Scheduler tick interval (seconds)',intervals,s.tick_seconds || 60,{min:1,max:31536000});
      this.recovery=preset(p,'max_recovery_attempts','Auto-recoveries per task',[['0','0'],['3','3'],['5','5']],s.max_recovery_attempts ?? 5,{min:0,max:10});
      let zones=Intl.supportedValuesOf ? Intl.supportedValuesOf('timeZone') : [];
      zones=[...new Set([r.timezone || 'UTC','UTC',...zones])];
      this.zone=selectField(p,'timezone','Timezone',zones.map(zone=>[zone,zone]),r.timezone || 'UTC');
    } else if(this.section==='learning') {
      const l=r.learning_policy || {};
      this.resumes=preset(p,'max_resumes','Auto-resumes per evaluation per day',[['0','0'],['3','3'],['5','5']],l.max_resumes ?? 3,{min:0,max:10});
      this.attempts=preset(p,'max_attempts','Attempts per analysis round',[['3','3'],['6','6'],['10','10']],l.max_attempts ?? 6,{min:1,max:20});
      this.calls=preset(p,'max_evaluation_calls','Call limit per evaluation',[['100','100'],['250','250'],['500','500']],l.max_evaluation_calls ?? 500,{min:1,max:500});
      this.methodLearning=selectField(p,'method_learning','Automatic method learning',[['false','Off'],['true','On']],String(r.method_learning ?? false));
    } else if(this.section==='execution') {
      this.plan=selectField(p,'plan_mode','Planning mode',[['auto','Auto'],['force','Forced planning'],['off','Direct execution']],r.plan_mode || 'auto');
      this.context=selectField(p,'context_plugin','Context plugin',[['','Service default']]);
      this.workflow=selectField(p,'workflow_plugin','Workflow plugin',[['','Service default']]);
      this.collections=element('div','external-choices');this.collections.setAttribute('aria-label','Tool collections');p.append(this.collections);
    } else if(this.section==='resources') {
      field(p,'paths','Source paths (one per line)',(r.source_routes?.[0]?.paths || []).join('\n'),{multiline:true});
      this.knowledge=element('div','knowledge-choices');this.tools=element('div','external-choices');
      this.knowledge.setAttribute('aria-label','Knowledge bases');this.tools.setAttribute('aria-label','External tools');p.append(this.knowledge,this.tools);
    }
  }
  async load(){
    if(!['models','execution','resources'].includes(this.section))return;
    this.submit.disabled=true;this.retry.hidden=true;this.error.textContent='';
    try{
      const catalog=await this.api.json('/v1/web/catalog',{signal:this.abort.signal});if(this.closed)return;
      const r=this.role;
      if(this.section==='models') {
        const models=(catalog.models || []).map(model=>[model.id,model.label || model.id]);
        choices(this.model,models,catalog.model_aliases?.[r.model] || r.model);
        choices(this.evaluation,models,catalog.model_aliases?.[r.evaluation_model] || r.evaluation_model);
      } else if(this.section==='execution') {
        choices(this.context,(catalog.plugins?.context || []).map(plugin=>[plugin.id,plugin.label || plugin.id]),r.task_spec?.context?.plugin);
        choices(this.workflow,(catalog.plugins?.workflow || []).map(plugin=>[plugin.id,plugin.label || plugin.id]),r.task_spec?.workflow?.plugin);
        this.collections.replaceChildren();
        const selected=new Set(r.task_spec?.tools?.collections || []),available=new Set();
        for(const row of catalog.tool_collections || []) {
          if(['knowledge','external_tools'].includes(row.id))continue;available.add(row.id);
          const label=element('label'),check=element('input');check.type='checkbox';check.value=row.id;check.checked=selected.has(row.id);
          label.append(check,document.createTextNode(row.label || row.id));this.collections.append(label);
        }
        for(const id of selected)if(!available.has(id) && !['knowledge','external_tools'].includes(id)) {
          const label=element('label'),check=element('input');check.type='checkbox';check.value=id;check.checked=true;
          label.append(check,document.createTextNode(`${id} (currently unavailable)`));this.collections.append(label);
        }
      } else {
        const bases=await this.api.json('/v1/knowledge-bases',{signal:this.abort.signal});if(this.closed)return;
        agentResourceChoices(this.tools,this.knowledge,catalog,bases,r);
      }
      this.submit.disabled=false;
    }catch(error){if(!this.closed && error.name!=='AbortError'){this.error.textContent=error.message;this.retry.hidden=false;}}
  }
  changes(){
    const f=this.form.elements,r=this.role;
    if(this.section==='identity')return {name:f.name.value.trim(),role_text:f.role_text.value.trim(),methods:lines(f.methods.value)};
    if(this.section==='models')return {model:this.model.value || null,evaluation_model:this.evaluation.value || null};
    if(this.section==='budget')return {budget:{daily_tokens:this.daily.value()}};
    if(this.section==='scheduling')return {interval_seconds:this.interval.value(),timezone:this.zone.value,
      scheduling_policy:{enabled:this.enabled.value==='true',tick_seconds:this.tick.value(),max_recovery_attempts:this.recovery.value()}};
    if(this.section==='learning')return {method_learning:this.methodLearning.value==='true',learning_policy:{
      max_resumes:this.resumes.value(),max_attempts:this.attempts.value(),max_evaluation_calls:this.calls.value()}};
    if(this.section==='resources') {
      const changes={source_routes:structuredClone(r.source_routes || []),knowledge_base_ids:selectedTools(this.knowledge),external_tool_ids:selectedTools(this.tools)};
      const paths=lines(f.paths.value);
      if(changes.source_routes.length)changes.source_routes[0].paths=paths;
      else if(paths.length)changes.source_routes.push({id:'implementation',topic:'Implementation sources',paths});
      for(const route of changes.source_routes)for(const key of ['knowledge_base_ids','external_tool_ids'])
        if(route[key])route[key]=route[key].filter(id=>changes[key].includes(id));
      return changes;
    }
    const spec=structuredClone(r.task_spec || {});spec.schema_version=1;
    const collections=selectedTools(this.collections);
    if(collections.length)(spec.tools ||= {}).collections=collections;
    else if(spec.tools)delete spec.tools.collections;
    for(const [kind,plugin] of [['context',this.context.value],['workflow',this.workflow.value]]) {
      if(plugin)spec[kind]={...(spec[kind] || {}),plugin};else delete spec[kind];
    }
    if(spec.tools && !Object.keys(spec.tools).length)delete spec.tools;
    return {plan_mode:this.plan.value,task_spec:Object.keys(spec).some(key=>key!=='schema_version')?spec:null};
  }
}
