/** Agent transport reuses authenticated SessionApi; command IDs survive a retry. */
export class AgentsApi {
  constructor(api) { this.api = api; }
  path(id, route = '') { return `/v1/agents${id ? `/${encodeURIComponent(id)}` : ''}${route ? `/${route}` : ''}`; }
  get(id, route = '', signal) {
    return this.api.conditionalJson ? this.api.conditionalJson(this.path(id, route), {signal}) : this.api.json(this.path(id, route), {signal});
  }
  async view(id, section, signal) {
    try{return await this.get(id,`views/${section}`,signal);}
    catch(error){if(error.status!==404)throw error;return this.get(id,'',signal);}
  }
  post(id, route, body, signal) { return this.api.json(this.path(id, route), {body, signal}); }
  command(id, type, revision, commandId = globalThis.crypto.randomUUID()) {
    return this.post(id, 'commands', {command_id: commandId, type, expected_revision: revision});
  }
}
