import {element} from '../markdown.mjs';
import {badge, panel, table} from './dashboard.mjs';

const button = (label, action) => {const b = element('button', 'quiet', label); b.type = 'button'; b.addEventListener('click', action); return b;};
const enc = encodeURIComponent;

export class SkillsView {
  constructor(root, {api, sessionId, signal}) {
    Object.assign(this, {root, api, sessionId, signal, query: '', busy: false});
    signal.addEventListener('abort', () => {clearTimeout(this.timer); this.dialog?.remove();}, {once: true});
  }
  path(suffix = '') {return `/v1/skills${suffix}?session_id=${enc(this.sessionId)}`;}
  async action(action) {
    if (this.busy) return;
    this.busy = true;
    try {await action(); if (!this.signal.aborted) await this.refresh();}
    catch (error) {if (!this.signal.aborted) {this.error = error.message; this.render();}}
    finally {this.busy = false;}
  }
  async open(query = '') {
    if (query) this.query = query;
    if (!this.data) this.root.replaceChildren(element('p', 'muted', '正在整理同 workspace 的案例目录…'));
    await this.refresh();
  }
  async refresh() {
    clearTimeout(this.timer);
    const generation = this.generation = (this.generation || 0) + 1;
    try {
      const data = await this.api.json(this.path(), {signal: this.signal});
      if (this.signal.aborted || generation !== this.generation) return;
      this.data = data; this.error = ''; this.render();
      if (data.jobs.some(j => ['queued', 'running'].includes(j.state))) this.timer = setTimeout(() => this.refresh(), 3000);
    } catch (error) {if (!this.signal.aborted && generation === this.generation) {this.error = error.message; this.render();}}
  }
  render() {
    this.opened = new Set([...this.root.querySelectorAll('details[data-version-id][open]')].map(n => n.dataset.versionId));
    this.root.replaceChildren();
    const top = element('div', 'dashboard-toolbar');
    top.append(element('h3', '', 'Skills · 跨任务归纳'), button('刷新', () => this.refresh())); this.root.append(top);
    if (this.error) this.root.append(element('p', 'notice error', this.error));
    if (!this.data) return;
    const {catalog, workspace} = this.data;
    this.root.append(element('p', 'muted', `Workspace: ${workspace}`), element('p', '',
      `${catalog.total_cases} 案例 · ${catalog.families} 去重家族 · ${catalog.uncertain_cases} 任务边界不确定（不计独立支持）`));
    const form = element('form', 'setup-block'), label = element('label', '', '要跨任务比较的问题或方法');
    const input = element('textarea'); input.value = this.query; input.required = true; input.maxLength = 4000;
    input.setAttribute('aria-label', '归纳问题'); input.addEventListener('input', () => {this.query = input.value;}); label.append(input);
    const model = element('select'); model.setAttribute('aria-label', '归纳模型');
    for (const m of this.data.models) {const option = element('option', '', m.label); option.value = m.id; model.append(option);}
    model.value = this.model || this.data.default_model; model.addEventListener('change', () => {this.model = model.value;});
    const budget = element('input'); budget.type = 'number'; budget.min = 1; budget.max = 12;
    budget.value = this.maxCalls || this.data.defaults.max_calls; budget.setAttribute('aria-label', '最大模型调用数');
    budget.addEventListener('input', () => {this.maxCalls = Number(budget.value);});
    const submit = element('button', '', '开始归纳'); submit.type = 'submit';
    form.append(label, model, element('span', 'muted', '最大模型调用数'), budget, submit);
    form.addEventListener('submit', event => {event.preventDefault(); this.action(() => this.api.json(this.path('/jobs'), {
      signal: this.signal, body: {query: input.value, model: model.value, settings: {max_calls: Number(budget.value)}}}));});
    this.root.append(form, element('p', 'muted small', '发现、验证与留出集在归纳前冻结。历史任务无法重建初始环境时，候选等待后续试用；模型审查不会代替执行验证。'));
    const jobs = panel(this.root, '归纳作业', true);
    for (const job of this.data.jobs) {
      const row = element('div', 'setup-block');
      row.append(element('strong', '', job.query), badge(job.state), element('p', 'muted small',
        `${job.stage} · ${job.usage.calls}/${job.settings.max_calls} calls · ${job.usage.tokens.toLocaleString()} tokens（含未报告预留）`));
      if (job.error) row.append(element('p', 'notice', job.error));
      if (job.state === 'failed' && !job.version_id) {
        row.append(element('p', 'notice', '归纳失败，尚未生成 Skill 版本。已保留完成阶段，可恢复继续。'));
        const size = job.candidate_diagnostic;
        if (size?.characters > size?.maximum) row.append(element('p', 'notice',
          `方法草稿 ${size.characters.toLocaleString()} 字符，超过 ${size.maximum.toLocaleString()} 字符上限；需要压缩后重新审查。`));
      }
      if (['failed', 'cancelled', 'interrupted'].includes(job.state)) row.append(button('恢复', () => this.action(() => this.api.json(
        this.path(`/jobs/${enc(job.id)}/resume`), {signal: this.signal, body: {}}))));
      if (['running', 'queued'].includes(job.state)) row.append(button('停止', () => this.action(() => this.api.json(
        this.path(`/jobs/${enc(job.id)}/cancel`), {signal: this.signal, body: {}}))));
      jobs.append(row);
    }
    if (!this.data.jobs.length) jobs.append(element('p', 'muted', '尚无归纳作业。'));
    const versions = panel(this.root, 'Skill 版本与证据', true);
    for (const version of this.data.versions) this.renderVersion(versions, version);
    if (!this.data.versions.length) versions.append(element('p', 'muted', '候选将在有界归纳和反例审查后显示。'));
    this.root.append(button('查看案例目录（纳入发现暴露记录）', () => this.action(async () => {
      this.cases = await this.api.json(this.path('/cases'), {signal: this.signal});
    })));
    if (this.cases) {
      const rows = panel(this.root, '案例目录', true);
      for (const c of this.cases.cases) rows.append(element('p', '', `${c.record?.title || c.task_contract.objective} · ${c.task_family_id} · ${c.outcome.local_result}`));
      if (this.cases.next_cursor) rows.append(button('下一页', () => this.action(async () => {
        this.cases = await this.api.json(this.path('/cases') + `&after=${enc(this.cases.next_cursor)}`, {signal: this.signal});
      })));
    }
  }
  renderVersion(root, version) {
    const node = element('details', 'setup-block'), head = element('summary');
    node.dataset.versionId = version.id; node.open = this.opened.has(version.id);
    head.append(element('strong', '', version.contract?.title || version.id), badge(version.state), badge(version.evidence_state || '来源不可用'));
    node.append(head); root.append(node);
    if (!version.sources_available) {node.append(element('p', 'notice', '来源已删除、撤销或修订。此版本已停止自动采用，证据内容不再导出。')); return;}
    const c = version.contract;
    node.append(element('p', '', c.summary), element('p', 'muted', `${version.coverage.discovery_families} 独立发现家族 · ${version.coverage.condition_variations} 条件配置`),
      element('p', '', `触发条件：${c.trigger}`), element('p', '', `前置条件：${c.preconditions.join('；')}`));
    table(node, ['命题', '依据', '支持家族', '反例家族', '未知'], c.claims.map(claim => [claim.text, claim.basis,
      claim.supporting_family_ids.length, claim.contradicting_family_ids.length, claim.unknown.length]));
    const steps = element('ol'); for (const step of c.steps) steps.append(element('li', '', step.instruction)); node.append(steps);
    for (const claim of c.claims) {
      const evidence = element('details'); evidence.append(element('summary', '', claim.text));
      for (const ref of claim.evidence_refs) {
        evidence.append(element('blockquote', '', `${ref.role} · ${ref.text}`), button(`原始证据 · ${ref.family_id} · L${ref.ref.line_number}`, () => this.showEvidence(ref)));
      }
      node.append(evidence);
    }
    for (const gap of [...c.gaps, ...version.limitations, ...c.blocking_issues]) node.append(element('p', 'notice', typeof gap === 'string' ? gap : gap.reason));
    const actions = element('div', 'dashboard-toolbar');
    const change = state => this.action(() => this.api.json(this.path(`/versions/${enc(version.id)}/state`),
      {signal: this.signal, body: {state, revision: version.revision}}));
    if (['candidate', 'trial'].includes(version.state)) actions.append(button('显式试用下一次 run', () => this.action(() => this.api.json(
      this.path(`/versions/${enc(version.id)}/trial`), {signal: this.signal, body: {revision: version.revision}}))));
    if (version.validations?.some(v => v.publication_passed)) actions.append(button('发布为 active', () => change('active')));
    if (['candidate', 'trial', 'active'].includes(version.state)) actions.append(button('暂停采用', () => change('suspended')));
    if (version.state !== 'retired') actions.append(button('退役', () => change('retired')));
    actions.append(button('重新归纳', () => this.action(() => this.api.json(this.path('/jobs'), {
      signal: this.signal, body: {query: c.trigger, parent_version_id: version.id}}))));
    actions.append(button('下载 Skill 包', () => this.action(async () => {
      const data = await this.api.json(this.path(`/versions/${enc(version.id)}/package`), {signal: this.signal});
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: 'application/json'}));
      const a = element('a'); a.href = url; a.download = `${version.id.replace(':', '-')}.json`; a.click(); URL.revokeObjectURL(url);
    })));
    node.append(actions, element('p', 'muted small', `${version.uses?.length || 0} 条最近使用记录 · ${version.validations?.length || 0} 份执行验证`));
    for (const validation of version.validations || []) node.append(element('pre', 'evidence-text', JSON.stringify(validation, null, 2)));
    for (const use of version.uses || []) node.append(element('p', 'muted small', `${use.kind} · ${use.run_id}`));
  }
  async showEvidence(evidence) {
    this.dialog?.remove();
    const dialog = element('dialog', 'evidence-dialog'), text = element('pre', 'evidence-text', 'Loading…');
    dialog.append(button('关闭', () => dialog.close()), text); this.root.append(dialog); this.dialog = dialog; dialog.showModal();
    try {
      const source = evidence.source;
      const version = this.data.versions.find(v => v.contract?.claims.some(c => c.evidence_refs.includes(evidence)));
      const claim = version?.contract.claims.find(c => c.evidence_refs.includes(evidence));
      // Session identity is host-materialized alongside the original pointer.
      const page = await this.api.trajectoryEvidence(source.session_id, source.analysis_id, evidence.ref,
        evidence.ref.start || 0, this.signal, Math.min(8000, (evidence.ref.end || 8000) - (evidence.ref.start || 0)));
      if (!this.signal.aborted) text.textContent = `${claim?.text || ''}\n${page.content}`;
    } catch (error) {if (!this.signal.aborted) text.textContent = error.message;}
  }
}
