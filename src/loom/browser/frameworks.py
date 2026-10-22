"""Read bounded scene facts from an already loaded framework module, without app edits."""

import hashlib

R3F_PROBE = r"""async () => {
  const resources = performance.getEntriesByType('resource');
  const entry = resources.find(e => {
    const u = new URL(e.name, location.href);
    return u.origin === location.origin && u.pathname.endsWith('/@react-three_fiber.js');
  });
  if (!entry) return null;
  try {
    const mod = await import(entry.name), root = mod._roots?.get(document.querySelector('canvas'));
    const state = root?.store?.getState();
    if (!state?.scene) return null;
    const meshes = [], geometry_counts = Object.create(null), texture_occurrences = Object.create(null);
    const pending = [[state.scene, true]];
    let inspected = 0, mesh_count = 0, counts_complete = true;
    const increment = (target, key, limit) => {
      if (Object.hasOwn(target,key) || Object.keys(target).length < limit) target[key] = (target[key] || 0) + 1;
      else counts_complete = false;
    };
    while (pending.length && inspected < 4096) {
      const [node, inherited] = pending.pop(); inspected++;
      const visible = inherited && node.visible !== false;
      for (const child of node.children || []) pending.push([child, visible]);
      if (!node.isMesh || !node.geometry) continue;
      mesh_count++;
      const geometry = String(node.geometry.type || 'unknown').slice(0, 64);
      if (visible) increment(geometry_counts,geometry,32);
      const material = Array.isArray(node.material) ? node.material[0] : node.material;
      const image = material?.map?.image;
      let texture = null;
      if (typeof image?.src === 'string' && /^https?:/.test(image.src))
        texture = new URL(image.src).pathname.slice(-240);
      if (visible && texture) increment(texture_occurrences,texture,64);
      if (meshes.length < 32) {
        const m = node.matrixWorld?.elements;
        meshes.push({uuid:String(node.uuid || '').slice(0, 40),name:String(node.name || '').slice(0, 80),
          geometry,visible_in_scene:visible,material_side:Number.isFinite(material?.side) ? material.side : null,texture,
          geometry_radius:Number.isFinite(node.geometry.parameters?.radius) ? node.geometry.parameters.radius : null,
          world_position:m ? [m[12],m[13],m[14]].map(v => Number.isFinite(v) ? +v.toFixed(4) : null) : null});
      }
    }
    return {probe:'react_three_fiber_loaded_module_v1',assurance:'runtime_scene_observation_not_pixel_proof',
      complete:pending.length === 0 && counts_complete,inspected_nodes:inspected,mesh_count,details_truncated:mesh_count > meshes.length,
      visible_geometry_counts:geometry_counts,visible_texture_occurrences:texture_occurrences,meshes};
  } catch (_) { return {probe:'react_three_fiber_loaded_module_v1',status:'unavailable'}; }
}"""


async def framework_facts(page):
    import asyncio

    try:
        async with asyncio.timeout(2):
            result = await page.evaluate(R3F_PROBE)
            return {**result, 'probe_sha256': hashlib.sha256(R3F_PROBE.encode()).hexdigest()} if result else None
    except TimeoutError:
        return {'probe': 'react_three_fiber_loaded_module_v1', 'status': 'timeout'}
