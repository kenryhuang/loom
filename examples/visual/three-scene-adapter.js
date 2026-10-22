// A workspace adapter is an application-reported observation, never a trusted oracle.
// Expose the actual THREE scene/camera/renderer as window.__LOOM_VISUAL_SCENE__.
// Optional pause/overview callbacks should operate the app's real controls/state.
// Use this function expression with scenario.adapter_path; it imports nothing.
({operation}) => {
  const hook = window.__LOOM_VISUAL_SCENE__;
  if (!hook?.scene || !hook?.camera || !hook?.renderer) {
    throw new Error('Visual scene hook unavailable: expose the actual scene, camera and renderer');
  }
  if (operation === 'pause' || operation === 'overview') {
    if (typeof hook[operation] !== 'function') throw new Error(`Scene operation unavailable: ${operation}`);
    hook[operation]();
    return {operation, completed: true};
  }
  if (operation !== 'observe') throw new Error('Unsupported visual adapter operation');
  const bodies = [];
  hook.scene.traverse(node => {
    // Tag a semantic body root once, not every constituent ring/glow/mesh.
    const id = node.userData?.celestialBodyId;
    if (!id) return;
    let visible = true;
    for (let ancestor = node; ancestor; ancestor = ancestor.parent) visible &&= ancestor.visible;
    bodies.push({id, object_uuid: node.uuid, kind: node.userData?.celestialBodyKind || 'unknown', visible_in_hierarchy: visible});
  });
  const ids = bodies.map(body => body.id);
  return {
    assurance: 'application_reported', scope: 'scene_graph_membership_not_pixel_visibility',
    bodies, duplicate_ids: ids.filter((id, index) => ids.indexOf(id) !== index),
    camera: {position: hook.camera.position.toArray(), quaternion: hook.camera.quaternion.toArray()},
    renderer: {calls: hook.renderer.info.render.calls, triangles: hook.renderer.info.render.triangles},
    limitations: ['Object membership does not establish frustum visibility or lack of occlusion; inspect real screenshots too.'],
  };
}
