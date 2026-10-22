# Web / Canvas / WebGL 视觉观察与验收

Loom 可以实际打开应用、操作页面、采集 PNG 截图，并通过独立的多模态模型验收画面。数量/文本/显隐等浏览器断言与视觉判断分别保留，模型不能覆盖明确失败的断言。

## 启用

安装可选浏览器依赖与 Chromium。`--inexact` 保留当前环境里已安装的其他可选依赖；浏览器只在安装阶段下载，任务执行时不自动安装。

```bash
uv sync --extra browser --inexact
PLAYWRIGHT_SKIP_BROWSER_GC=1 .venv/bin/python -m playwright install chromium
```

在现有百炼 provider 下增加模型，并设置视觉模型角色：

```yaml
visual_verification_model: dashscope/qwen3.8-omni-flash
providers:
  dashscope:
    # 复用当前地域/业务空间已经验证通过的连接和密钥变量。
    base_url: https://YOUR-VERIFIED-ENDPOINT/compatible-mode/v1
    api_key_env: LOOM_LLM_API_KEY
    models:
      qwen3.8-omni-flash:
        input_modalities: [text, image]
        context_window_tokens: 1000000
        max_completion_tokens: 4096
        request_options:
          modalities: [text]
          reasoning_effort: none
          response_format:
            type: json_object
```

也可以在 Settings → Models 配置 Input modalities，在 General 选择 Visual acceptance model。角色必须指向配置中声明支持 image 的模型。Qwen3.8-Omni-Flash 使用 JSON Mode，输出由 Loom 本地校验；它不走 native JSON Schema 模式。

New session 选择 **Browser & visual evidence**；本地应用同时需要 Files、Commands 和 workspace。LLM 推荐会为页面/WebGL 缺陷建议浏览器集合。普通任务不会自动启动浏览器。

运行中的后端需要重启才能载入新增工具/RPC 代码。已有 session 若未绑定浏览器，可通过编辑任务选择该集合后 Save & restart；历史消息与结果继续保留为背景，不直接改写历史验收为通过。

## 工具与场景

| 工具 | 行为 |
| --- | --- |
| `browser_open` | 打开一个隔离的交互页面，返回 page_id；新 open 替换前一个交互页面 |
| `browser_act` | click/fill/select/press/scroll/drag/wait_visible/wait_text/adapter 等有界动作 |
| `browser_observe` | 当前页面截图、浏览器断言、DOM 摘要、Canvas/WebGL 状态和可选视觉描述 |
| `browser_capture` | 在独立 worker/context 中重放完整场景，采证后回收预览与浏览器；验收使用此工具 |
| `browser_close` | 提前关闭交互浏览器和预览，释放资源 |

page_id 不跨恢复持久化。恢复后需要重新 open，不能把旧页面 ID 当作仍然存活的资源。预览和交互经过已有 runtime、操作日志、控制和取消机制；未知结果的交互继续走操作核对，不自动当作成功重放。

本地应用的场景示例：

```json
{
  "id": "solar-default-view",
  "preview": {
    "argv": ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "{port}"],
    "cwd": ".",
    "route": "/"
  },
  "ready_selector": "canvas",
  "canvas": true,
  "captures": [
    {"id": "overview", "selector": "canvas", "after_ms": 1500},
    {"id": "later", "selector": "canvas", "after_ms": 1000}
  ]
}
```

预览命令根据项目实际入口选择；`{port}` 由宿主分配。使用当前代码的有界副本启动预览，避免已有 dev server 版本不明。依赖包共享，工具缓存隔离；源码、锁文件、场景和 adapter 都进入证据绑定，不跨 run 缓存视觉通过结果。

`preview` 与 `url` 必须二选一，原生结构化输出中未使用的一项填 `null`。可选字段的 `null` 会在本地校验前记录并移除，不能用空字符串或虚构路径代替。预览命令缺少 `{port}` 会在计划校验时拒绝；`adapter_path` 只能填写真实的 adapter 函数文件，不能填写应用入口文件。

源码副本跳过依赖、构建输出、隐藏文件和秘密文件；当前上限为 4096 个文件、单文件 8 MB、总计 64 MB。不能完整建立绑定时明确阻塞，不截断后宣称对应当前候选。使用隐藏配置或特殊构建入口的应用需要提供适用的场景准备方式；不要把目标改成不相关的空页面。

外部 URL 必须通过 session 的 URL resource 显式绑定；采集只访问目标及已绑定 origin。本地交互调试允许 workspace 下的 loopback 页面；本地正式验收使用候选预览，或使用明确绑定的外部目标。URL 相同或 HTTP 200 不证明它与当前源码对应。

## 确定性检查与 adapter

DOM 场景可以附带断言，例如：

```json
{
  "assertions": [
    {"id": "one-heading", "kind": "count", "selector": "h1", "expected": 1},
    {"id": "ready-text", "kind": "text", "selector": "#ready", "expected": "Ready"},
    {"id": "no-loader", "kind": "hidden", "selector": "#loading"}
  ]
}
```

这不适用于通过 DOM 计算 3D 天体数。精确的场景对象检查应使用项目已有测试、运行时 adapter 与实际截图交叉验证。

`adapter_path` 指向 workspace 中最多 24 KB 的 JavaScript 函数表达式，例如 `tests/visual/scene-adapter.js`。函数接收 `{operation}`；观察时宿主调用 `observe`，准备动作通过 `{"action":"adapter","value":"pause"}` 等显式记录。adapter 文件 hash 进入证据，不能在判定期间替换。

[Three.js adapter 示例](../examples/visual/three-scene-adapter.js) 展示如何读取实际 scene graph、相机和渲染器信息。应用须显式暴露对应场景 hook。adapter 输出标记为 `application_reported`，不是独立的像素事实；不能仅返回期望常量然后当作正确证明。

对于已加载可识别模块的 React Three Fiber 页面，宿主还会读取其实际 root store / scene，记录网格类型、父级显隐、纹理引用次数和世界位置，连同 probe 版本/hash 写入每帧证据。当前自动识别范围为同源已加载的 `@react-three_fiber.js` 模块（如 Vite 预览）；其他打包方式继续使用显式 adapter。遍历与结果有界，截断会明确标记。它不会修改应用源码，也不会把场景对象数自动当作屏幕可见对象数；星空背景球与轨道网格须结合纹理、材质和截图区分。

浏览器通过观察应用实际的 getContext 和绘制调用记录 WebGL 状态，不为探测而提前创建应用 context。截图前最多等待 12 秒：先等待文档与资源加载趋于稳定，再等待可见 Canvas 初始化并实际提交渲染；可识别的 React Three Fiber 场景还会等待空 root 挂载内容，避免 Suspense 加载纹理时的黑屏。就绪状态、耗时、超时阶段、失败资源请求与 HTTP 错误都进入证据；就绪不等于视觉验收通过。超时仍保留实际画面和诊断，不伪造内容或隐藏真实黑屏。

截图不默认关闭动画；持续 rAF/WebGL 使用多个明确采样时刻。需要固定模拟时间、随机种子或相机时，通过场景中可审计的应用操作实现。`ready_selector` 只证明元素可见，特殊应用仍可提供更严格的等待动作或 adapter。

未指定 `captures` 时，在完成就绪等待后再识别页面类型。显式要求 Canvas 或此时观察到 WebGL 时，默认延时 1.5 秒采第一张、再间隔 1 秒采第二张，避免根据初始化前的空 Canvas 误选普通网页单帧。实际采样时刻均保留；这不能替代应用自己的就绪信号。

## 验收与恢复

验收计划使用 `verifier: visual`，`check: {scenario: ...}`。宿主独立重放，图像发送给视觉模型；执行 agent 的视觉描述不直接计作通过。

- 浏览器断言失败：返回 `failed`，进入已有修复循环。
- 模型发现明确画面缺陷：保留截图、归一化区域与理由，返回 `failed`。
- 视角/帧数不足：在同一允许场景内最多补采一次；仍不足返回 `insufficient_visual_evidence`。
- 浏览器、图像模型、预览或版本绑定不可用：具体 blocked 原因。
- 条件满足且输入保持有效：与其他检查进入最终完整目标 gate。

每轮最多 4 张图、每图 4 MB、最多两次采集；动作/截图/模型调用同时受工具超时和已有验收/任务总预算约束。图像 token 单独估算，真实用量按 provider 返回计入任务。修复后重新采图，不复用旧画面来验收新源码。

### 验收额度与 Resume

界面分别显示 Input budget（单次上下文容量）和 Acceptance budget（验收调用次数、耗时、失败候选次数），两者不是同一额度。例如 `5,218 / 600,000 tokens` 不说明验收还有模型调用额度。

普通任务的默认验收额度是 6 次模型调用、180 秒。含视觉条件时，未显式设置的默认值会覆盖多次采集、格式修复和候选修复：一项视觉条件、默认两次修复的额度为 20 次调用、600 秒。task spec 中显式设置的 `acceptance.max_model_calls` / `max_seconds` 始终保留；所有调用同时受任务整体的模型调用、token 和时间限制约束。真正判定候选失败才消耗修复次数，暂停、模型/环境阻塞和预算不足不算作候选修复失败。

明确点击 **Resume** 时，宿主会将已阻塞验收的额度、结果和原因保存为历史 artifact，再开启新的有界验收额度。任务总用量、原始要求、验收计划和待核对的未知操作保留；必须重新验证才能通过。单纯重启服务、执行器再次调用 finish 或修改验收方法不会补充额度。界面可查看上一份额度记录，历史 Processing 记录也继续保留。

已知验收调用额度不足时，在启动新命令或浏览器前返回具体的调用/时间用量及恢复说明，不再先采图后才发现无法判断。

旧计划将「当前不能目视」混入 unresolved 时，新增浏览器和视觉能力可以触发一次方法迁移：保留原条件，提出视觉条件，独立复核被替换限制与原用户目标的对应关系，保存历史及迁移证据，再执行新验收。明确人工/客户审批或不确定要求仍保留。迁移本身不产生通过。

相同未解决阻塞且相关输入/能力没有变化时，不再次消耗相同验收轮数。外部页面状态和无法判断的视觉内容仍可能需要新的观察；不能将模型的置信度当作自动放行阈值。

## 查看证据与验证实现

Web 的 Processing / 验收记录中可以展开截图证据，点击 View screenshot 查看原图及判断区域。图片通过带认证的 session artifact API 读取，不在 URL 放 credential。日志和 checkpoint 仅含引用；图片只在请求边界转换为模型图像输入。

区域标注是可选信息。模型给出无法可靠解释的像素坐标或越界坐标时，宿主记录原值并省略该叠层，保留原始观察文字和判定，不猜测坐标系，也不会仅因标注格式再次付费请求整个视觉判断。

```bash
.venv/bin/python -m pytest tests/browser tests/service/test_visual_artifacts.py -q
LOOM_VISUAL_LIVE=1 .venv/bin/python -m pytest tests/browser/test_live_visual.py -q
```

第二条显式调用本地配置中的真实视觉模型，验证单次请求确实识别两张不同颜色图像、返回有效 JSON，并覆盖流式/非流式，以及浏览器观察的普通文本描述。观察描述使用不带 `response_format` 的私有 provider 副本，正式验收继续使用 JSON Mode；两者不改写共享配置。它证明协议与连通性，不能替代复杂视觉任务的质量评估。

首期支持 PNG、Chromium、结构化页面场景和 Canvas/WebGL 多帧。桌面/PDF/完整视频、像素基线比较及复杂认证 fixture 尚未启用。共享依赖和外部环境仍有观测边界，结果说明实际视角、时刻和证据强度。
