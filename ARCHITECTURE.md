# CY-Translator 架构分析报告

> 生成日期：2026-05-17

***

# 1. 文件结构总览

## 入口

| 文件        | 职责                           |
| --------- | ---------------------------- |
| `main.py` | 应用程序入口，设置 Qt 环境变量，启动 MainApp |

## UI 层 (`ui/`)

| 文件                 | 职责                                                       |
| ------------------ | -------------------------------------------------------- |
| `ui/main_app.py`   | 顶层工作区：PDF 查看器 + 右侧面板的水平分割布局，管理 PDF 打开历史和信号路由             |
| `ui/pdf_viewer.py` | **核心模块**：PDF 渲染、\_Word 提取、划词选择引擎、隔离域管理、笔记系统、TOC 目录面板     |
| `ui/reader_tab.py` | **右侧控制面板**：翻译触发逻辑、缓存查找/写入、句补全两阶段流程、LLM 调用调度，以及 URL 管理对话框 |

## 服务层 (`services/`)

| 文件                              | 职责                                            |
| ------------------------------- | --------------------------------------------- |
| `services/cache_store.py`       | 翻译缓存持久化：双层短语/句子缓存、坐标匹配、条目合并、残句自整理             |
| `services/sentence_analyzer.py` | 句子分析：句末检测、句补全扩展、句子切分、译文拆分、双栏坐标变换              |
| `services/note_store.py`        | 笔记持久化：加载/保存笔记 JSON 文件                         |
| `services/config_store.py`      | 全局配置持久化：URL/Key/Model 管理、Prompt 文件列表、PDF 历史记录 |
| `services/file_writer.py`       | 翻译结果写文件：新建/追加 Markdown 文件                     |
| `services/llm_client.py`        | LLM API 客户端：通过 `/v1/chat/completions` 端点调用翻译  |
| `services/model_fetcher.py`     | 模型列表获取：通过 `/v1/models` 端点拉取可用模型               |
| `services/prompt_loader.py`     | 系统提示词加载：从 .txt 文件读取 prompt 内容                 |

## 工具层 (`utils/`)

| 文件                       | 职责                       |
| ------------------------ | ------------------------ |
| `utils/paste_cleaner.py` | 粘贴文本清洗：去除 PDF 复制产生的多余换行符 |

## 配置与提示词

| 文件                        | 职责                                   |
| ------------------------- | ------------------------------------ |
| `config.json`             | 全局配置文件（URL、Key、模型、提示词历史、PDF 历史、窗口尺寸） |
| `prompt.txt`              | 默认翻译提示词（纯文本输出）                       |
| `prompt_for_markdown.txt` | Markdown 格式翻译提示词                     |
| `requirements.txt`        | Python 依赖：PySide6、PyMuPDF、requests   |

***

# 2. 核心数据结构

## 2.1 \_Word 对象

**定义位置：** `ui/pdf_viewer.py:72-91`，`@dataclass`

```python
@dataclass
class _Word:
    idx: int          # 在全局 _words 列表中的索引，每次 _extract_words() 重新分配
    page_idx: int     # 所属页码（0-based）
    x0_pct: float     # 左边界百分比坐标 (0.0 ~ 1.0)
    y0_pct: float     # 上边界百分比坐标 (0.0 ~ 1.0)
    x1_pct: float     # 右边界百分比坐标 (0.0 ~ 1.0)
    y1_pct: float     # 下边界百分比坐标 (0.0 ~ 1.0)
    text: str         # 单词文本
    size: float       # 字号（来自 span 字典），0.0 表示未知
    flags: int        # 字体标记（来自 span 字典），bit 4 (16) 表示粗体
```

**坐标系**：百分比坐标，以页面宽高为分母。`center_x` 和 `center_y` 是计算属性，返回 bbox 几何中心。

**双栏变换**：\_Word 的百分比坐标始终存储为**物理坐标**（即 PDF 原始布局）。双栏逻辑变换在**使用处**进行（见 `_lx()`, `_ly()` 等 helper 函数），不修改 \_Word 本身。

**生命周期**：

1. `_extract_words()` 创建所有 \_Word 对象 → 存入 `self._words` 列表
2. 每次切换单/双栏或打开新 PDF 时重建
3. 窗口 resize 不会重建（因为坐标是百分比，与渲染分辨率无关）

## 2.2 \_Zone（隔离域）

**定义位置：** 存储为 `dict`，无显式 dataclass。

**字段：**

```python
{
    "page": int,   # 页码（0-based）
    "x0": float,   # 左边界百分比 (0.0 ~ 1.0)
    "y0": float,   # 上边界百分比
    "x1": float,   # 右边界百分比
    "y1": float,   # 下边界百分比
}
```

**存储**：`self._zones: list[dict]`（物理百分比坐标，与 scene 坐标通过 `_zone_scene_rect()` / `_scene_rect_to_zone()` 互转）。

**持久化**：JSON 数组，直接序列化到 `_isolate` 文件。

**生命周期**：

1. 加载 PDF 时从 isolate 文件读入
2. 用户框选时追加
3. 管理模式下可拖动/删除
4. 每次变更后自动保存到文件

## 2.3 Cache 条目

**定义位置：** `services/cache_store.py`

**顶层结构：**

```json
{
  "format_version": 2,
  "<pdf_absolute_path>": {
    "single": { "phrases": [...], "sentences": [...] },
    "dual":   { "phrases": [...], "sentences": [...] }
  }
}
```

### Phrase 条目

```json
{
  "src": "原文",
  "tgt": "译文"
}
```

按 `src` 精确匹配，用于短词组（≤5 词且无句末标点）。

### Sentence 条目

```json
{
  "src": "完整原文（所有子句拼接）",
  "tgt": "完整译文（所有子句译文拼接）",
  "head_fragment": false,
  "tail_fragment": false,
  "sentences": [
    {
      "start_page": 8, "start_x_pct": 0.146, "start_y_pct": 0.849,
      "end_page": 8,   "end_x_pct": 0.454,   "end_y_pct": 0.869,
      "src": "子句原文",
      "tgt": "子句译文",
      "is_head_fragment": true,
      "is_tail_fragment": false
    }
  ]
}
```

**sub 条目**: 每个 sub 是一个子句，包含起止坐标、原文、译文、以及 `is_head_fragment` / `is_tail_fragment` 标记（表示该子句是否为残句的首/尾）。

## 2.4 Notes 条目

**定义位置：** `services/note_store.py`

**存储结构：**

```json
{
  "font_size": 12,
  "notes": [
    {
      "page": 0,
      "x_pct": 0.809,
      "y_pct": 0.584,
      "text": "笔记内容",
      "width": 200,
      "height": 120
    }
  ]
}
```

**字段含义：**

- `page`: 所在页码（0-based）
- `x_pct`, `y_pct`: 笔记图标左上角的百分比坐标
- `text`: 笔记文本内容（纯文本）
- `width`, `height`: 弹出编辑器的尺寸（像素，在 scene 坐标系中）

***

# 3. 核心模块详细分析

## 3.1 PDF 解析与坐标系

### PyMuPDF 原始数据如何提取为 \_Word 对象

**位置：** `pdf_viewer.py:_extract_words()` (line 805-862)

流程：

```
page.get_text("dict") → 提取 span 信息 (size, flags)
         │
         ▼
page.get_text("words") → [(x0, y0, x1, y1, text, ...), ...]
         │
         ▼
  坐标归一化: x_pct = x / page_width, y_pct = y / page_height
         │
         ▼
  构造 _Word(idx, page_idx, x0_pct, y0_pct, x1_pct, y1_pct, text, size, flags)
         │
         ▼
  如果双栏模式: _reorder_dual_column() 重排 → 重新分配 idx
```

span 信息通过 `(x0, y0)` 最近邻匹配关联到对应 word，提供字号和粗体标记。

### 百分比坐标系的定义和换算

- **百分比坐标 = 物理位置 / 页面尺寸**，值域 \[0, 1]
- **坐标存储**：`_Word` 的 `x0_pct` / `y0_pct` / `x1_pct` / `y1_pct` 存储百分比坐标
- **zone** 也存储百分比坐标
- **cache sub 条目** 存储百分比坐标（双栏模式下，是变换后的逻辑坐标）

**Scene 坐标换算**（`pdf_viewer.py`）：

```python
# 百分比 → scene
scene_x = word.x0_pct * available_width
scene_y = page_offset_y + word.y0_pct * page_height_pts * scale_factor

# scene → 百分比（_scene_to_pdf）
x_pct = scene_x / available_width
y_pct = (scene_y - page_offset_y) / (page_height_pts * scale_factor)
```

### 双栏模式下逻辑坐标的变换规则

**变换时机**：仅在**使用处**进行，不修改原始数据。

**变换公式**（`pdf_viewer.py:_get_selected_words()` 内部 helper）：

```python
# 逻辑 center X: 右栏单词 x 坐标左移 0.5
def _lx(w): return w.center_x - 0.5 if (dual and w.center_x >= 0.5) else w.center_x

# 逻辑 Y: page * 2.0 + y0_pct, 右栏 +1.0
def _ly(w):
    base = w.y0_pct + w.page_idx * 2.0
    return base + 1.0 if (dual and w.center_x >= 0.5) else base
```

**语义**：将双栏 PDF 的右栏映射到"逻辑坐标空间"，消除左右栏之间的 Y 轴重叠。变换后：

- 左栏词：Y 范围 `[page*2, page*2+1)`
- 右栏词：Y 范围 `[page*2+1, page*2+2)`
- 左右栏在逻辑空间中垂直分离，不会相互干扰

**cache 中的变换**：`sentence_analyzer.py:transform_dual_column_coords()` 对 sub\_sentence 的坐标做同样变换后再写入缓存，确保缓存中的坐标与划词逻辑坐标一致。

**何时变换**：

- 划词选择（`_get_selected_words`）：总是使用逻辑坐标进行过滤
- 缓存匹配（`find_overlapping_entries` / `find_containing_entries`）：使用已变换后的坐标（因为写入缓存前已变换）
- 视觉渲染高亮（`_draw_highlights`）：使用物理坐标（因为渲染需要真实位置）
- 隔离域：使用物理坐标

### page\_offsets 的作用和计算方式

**定义**：`self._page_offsets: list[float]`，每个页面左上角在 `QGraphicsScene` 中的 Y 坐标。

**计算**（`_do_render_pages()`）：

```python
offset_y = 0.0
for page_idx in range(total_pages):
    ph = page.rect.height           # PDF points
    sf = available_width / ph       # scale factor
    scene_h = ph * sf                # page height in scene pixels
    page_offsets.append(offset_y)
    offset_y += scene_h + PAGE_GAP   # PAGE_GAP = 16
```

**用途**：

1. 将百分比坐标转换为 scene 坐标（Y 轴加 offset）
2. 确定鼠标点击落在哪个页面
3. 页面导航时的目标位置计算

## 3.2 划词引擎

### \_snap\_start / \_snap\_end 的工作流程

**`_snap_start(pos)`** (line 1306)：

```
1. 如果处于阅读模式且有隔离域
   → 判断鼠标是否在某个 zone 内
   → 在 zone 内 → _active_words = _words_inside
   → 在 zone 外 → _active_words = _words_outside
2. 否则 _active_words = _words（全部词）
3. 调用 _word_at_scene_pos(pos) 找到最近的词索引 → _start_idx
```

**`_snap_end(pos)`** (line 1327)：

```
1. 调用 _word_at_scene_pos(pos) 找到最近的词索引 → _end_idx
```

### \_word\_at\_scene\_pos 的命中算法

**位置**：`pdf_viewer.py:1250-1302`

```
Step 0: 候选池 = _active_words（由 _snap_start 锁定）
Step 1: scene_pos → PDF 百分比坐标 (_scene_to_pdf)
Step 2: 过滤候选池为同页词（如果没有则保留全部候选）
Step 3: 双栏模式下，按鼠标 X 坐标过滤左右栏
Step 4: 找与鼠标 Y 最近邻的词作为 ref（Y 轴区间距离）
Step 5: 收集与 ref 同行（Y 区间重叠度 ≥ 50%）的词
Step 6: 在同行词中找 X 最近邻 → 返回其索引
```

**同行判断（Step 5）**：使用 Y 轴区间重叠度算法，而非单点容差。条件是 `overlap >= 0.5 * min(height_a, height_b)`。

### \_get\_selected\_words 的完整流程

**位置**：`pdf_viewer.py:1346-1462`

这是划词引擎的核心函数，负责从两个锚点确定完整的选中词集合。

```
0. 锚点确定
   anchor_start = src[lo], anchor_end = src[hi]
   如果同行 → 按逻辑 X 排序确定 first/last
   如果跨行 → 按逻辑 Y center 排序确定 first/last

1. 缓冲池构建
   在 src 数组中，以 [lo, hi] 为中心向两侧各扩展 BUFFER(=20) 个词
   → candidate_words = src[buf_lo : buf_hi]

2. 锚点墙过滤（使用逻辑坐标）
   对于候选池中的每个词 w：
   ┌─ First anchor wall ─────────────────────────────
   │ 如果 w 的逻辑 Y center < first_anchor 的逻辑 Y center - LINE_TOLERANCE
   │   → 丢弃（w 在 first_anchor 上方太远）
   │ 如果 w 与 first_anchor 同行 且 w 的逻辑右边界 < first_anchor 的逻辑左边界
   │   → 丢弃（w 在 first_anchor 左侧）
   ├─ Last anchor wall ──────────────────────────────
   │ 如果 w 的逻辑 Y center > last_anchor 的逻辑 Y center + LINE_TOLERANCE
   │   → 丢弃（w 在 last_anchor 下方太远）
   │ 如果 w 与 last_anchor 同行 且 w 的逻辑左边界 > last_anchor 的逻辑右边界
   │   → 丢弃（w 在 last_anchor 右侧）
   └─ Spatial filter ────────────────────────────────
     如果 w 的逻辑 Y 不在 [y_min - tol, y_max + tol]
       → 丢弃

3. 几何重排
   对通过的词进行 _group_words_into_lines() 分行
   → 每行内按 x0_pct 排序
   → 拼接为最终结果
```

### 为什么需要几何重排

PDF 字符流顺序可能不是视觉阅读顺序（例如双栏 PDF 的右栏词在字符流中出现在左栏词之后；数学公式中的上下标可能以非视觉顺序出现）。几何重排确保最终输出文本的单词顺序与屏幕上看到的顺序一致。

### 同行判断算法

此处使用 `_same_line()` 函数，基于逻辑坐标的 Y 轴区间重叠度：

- 计算两个词在逻辑 Y 轴的区间重叠长度
- 判定条件：`overlap >= 0.5 * min(height_a, height_b)`
- 这是**区间重叠度**算法（不是单点容差），能正确处理不同字号的词

## 3.3 隔离域

### zones 的数据存储

**内存**：`self._zones: list[dict]`，每个 dict 的坐标是**物理百分比坐标**（不使用双栏逻辑变换）。

**文件**：JSON 数组，与内存结构一致。

### \_rebuild\_word\_lists 的预计算逻辑

**位置**：`pdf_viewer.py:940-962`

```python
def _rebuild_word_lists(self):
    遍历所有 _words:
      如果 word 的 bbox 完全被某个 zone 包含
        → 加入 _words_inside
      否则
        → 加入 _words_outside
```

**判定条件**：`word.page_idx == zone.page` 且 `word.x0_pct >= zone.x0` 且 `word.x1_pct <= zone.x1` 且 `word.y0_pct >= zone.y0` 且 `word.y1_pct <= zone.y1`

即：完全包含判定（所有四个边界都在 zone 内部）。

**触发时机**：

- 加载 zones 后
- 每次 zone 增删改后
- `_extract_words()` 末尾

### 划词时 \_active\_words 的切换时机

**切换发生在** **`_snap_start()`** **中**（line 1306-1322），即**鼠标按下时**：

1. 判断鼠标落点是否在某个 zone 的矩形内
2. 如果在 zone 内 → `_active_words = _words_inside`（只能选中 zone 内的词）
3. 如果在 zone 外 → `_active_words = _words_outside`（不能选中任何 zone 内的词）
4. 在非阅读模式或无 zone 时 → `_active_words = _words`（全部词）

**关键设计**：`_active_words` 在 `_snap_start` 中设置后就保持不变，整个拖拽过程中不会切换。这意味着**一次划词操作要么在 zone 内要么在 zone 外，不会跨越 zone 边界**。

### 与划词引擎、缓存的耦合关系

**结论：隔离域与缓存系统无交叉。**

- 隔离域通过 `_active_words` 影响划词引擎的候选池，不修改任何 word 对象本身
- `_active_words` 中的词索引与 `_words` 保持一致（因为 inside/outside 只是 `_words` 的子集，索引不变）
- 缓存中存储的坐标是 sub\_sentence 的起止坐标，来自 `_words` 中的实际 word 对象，不受隔离域影响
- 因此隔离域逻辑与缓存读写完全正交

## 3.4 自动句补全

### expand\_to\_sentence 的触发条件

**调用位置**：`reader_tab.py:_handle_sentence_auto_complete()` (line 677)

自动句补全仅当以下条件同时满足时触发：

1. `auto_complete` 开关为 ON
2. 选中词数 > 5（由 `classify()` 判定）

### 句末检测算法 (is\_sentence\_end)

**位置**：`sentence_analyzer.py:15-18`

```python
SENTENCE_ENDS = {'.', '。', '!', '？', '?'}

def is_sentence_end(text):
    s = text.strip()
    return bool(s) and s[-1] in SENTENCE_ENDS
```

**判定标准**：去掉末尾空白后，最后一个字符是否为句末标点。

**Chinese splitters**（用于译文拆分）还额外包括 `！`（全角感叹号）。

### head\_fragment / tail\_fragment 的判定

**位置**：`sentence_analyzer.py:expand_to_sentence()` (line 33-102)

```
Left scan (head_fragment):
  从 lo-1 向左扫描，限于当前页
  如果在某个词处遇到句末标点 → new_lo = i+1, head_fragment = False
  如果遇到段落边界 → 停止扫描（head_fragment 取决于是否找到过句末）
  如果扫描到页首仍未找到句末 → head_fragment = True（残句头）

Right scan (tail_fragment):
  如果 hi 已经是句末标点 → new_hi = hi, tail_fragment = False
  否则从 hi+1 向右扫描，限于当前页
  如果找到句末标点 → new_hi = i, tail_fragment = False
  如果扫描到页尾仍未找到 → tail_fragment = True（残句尾）
```

**段落边界判定**（`_is_para_boundary()`）：

- 两个连续词之间的 Y 间距 > 中位行间距 \* 1.1 → 认为是段落边界
- 仅在同页内判定

**基线统计**（从选中范围采集）：

- `median_size`：有字号信息的词的中位字号
- `bold_ratio`：粗体词比例
- `median_line_gap`：相邻词的 Y 间距中位数
- `x0_median`：起始 X 坐标中位数（用于检测缩进）

### ON 模式和 OFF 模式的差异

| 特性   | ON（自动句补全）                        | OFF（手动模式）                       |
| ---- | -------------------------------- | ------------------------------- |
| 分类阈值 | 词数 > 5 → sentence                | 含句末标点 → sentence                |
| 选区扩展 | 自动扩展 lo/hi 到句边界                  | 不扩展，保持用户划选范围                    |
| 缓存查找 | `find_overlapping_entries`（坐标重叠） | `find_containing_entries`（坐标包含） |
| 残句标记 | 来自 expand\_to\_sentence 的结果      | 直接判断 lo-1 和 hi 是否为句末            |
| 触发方式 | 自动（划词松开后立即触发）                    | 手动（右键菜单或自动翻译开关）                 |

**为什么 ON 用 overlapping 而 OFF 用 containing**：

- ON 模式选区被扩展到了完整句子边界，缓存条目也按完整句子存储，所以按坐标重叠查找即可匹配
- OFF 模式选区可能只是句子的一部分，需要查找完全"包含"该选区的缓存条目（即该句子的完整缓存）

## 3.5 公式元素回捞

### 为什么需要回捞

PDF 的字符流顺序（通过 `get_text("words")` 获得）是 PDF 内部编码顺序，可能与视觉阅读顺序不一致。典型的场景：

- 数学公式中的上下标字符在字符流中出现在正常文本之后
- 公式元素跨行分布

**"回捞"**：通过空间邻近性（而非字符流邻近性），将视觉上属于选中范围的公式元素纳入选区。

### 缓冲池的构建范围

```python
BUFFER = 20  # 在原始选中范围 lo/hi 两侧各扩展 20 个词
```

缓冲池从 `src`（即 `_active_words` 或 `_words`）中按索引范围截取：

```python
buf_lo = max(0, min(lo, hi) - BUFFER)
buf_hi = min(len(src), max(lo, hi) + BUFFER + 1)
candidate_words = src[buf_lo : buf_hi]
```

20 个词的缓冲区足够覆盖典型公式元素的偏移范围（通常不超过 1-2 行文字的宽度）。

### 锚点墙的完整判断逻辑

锚点墙（anchor walls）是 `_get_selected_words` 的核心过滤机制，消解了一个关键矛盾：缓冲池虽然扩大了候选范围，但不能让选区"无限制蔓延"。

```
first_anchor（选区的左上角锚点）
last_anchor （选区的右下角锚点）

First anchor wall → 阻挡"左上蔓延"：
  - 逻辑 Y 在 first_anchor 下方太远的词 → 丢弃
  - 与 first_anchor 同行但在其左侧的词 → 丢弃

Last anchor wall → 阻挡"右下蔓延"：
  - 逻辑 Y 在 last_anchor 上方太远的词 → 丢弃
  - 与 last_anchor 同行但在其右侧的词 → 丢弃
```

锚点墙确保：公式元素如果在锚点之间（空间上属于选区范围内）则被纳入；如果在锚点范围之外（属于上文或下文的其他内容）则被过滤掉。

### 几何重排的实现

**位置**：`_get_selected_words` 第 1457-1462 行

```python
lines = self._group_words_into_lines(retrieved)
result = []
for line_words in lines:
    line_words.sort(key=lambda w: w.x0_pct)  # 行内按 X 排序
    result.extend(line_words)
```

- 先用 `_group_words_into_lines` 将所有通过的词按 Y 轴聚类为行
- 每行内的词按 `x0_pct` 升序排列
- 拼接所有行得到最终结果

**为什么按 x0\_pct 而不是 x1\_pct 或 center\_x**：在从左到右的英文排版中，`x0_pct` 最接近视觉阅读顺序的左边界。

## 3.6 缓存系统

### 双层结构

```
cache
  └─ [file_path]
       ├─ single ─┬─ phrases: [{src, tgt}, ...]
       │           └─ sentences: [{src, tgt, head_fragment, tail_fragment, sentences: [sub, ...]}, ...]
       └─ dual   ─┬─ phrases: [...]
                   └─ sentences: [...]

format_version: 2
```

- **phrases**：精确字符串匹配，用于短词组翻译
- **sentences**：基于坐标范围匹配，用于句子级翻译，每个 entry 包含多个 sub\_sentence
- **single / dual**：单栏和双栏模式完全独立分组，互不干扰

### 坐标匹配的完整逻辑

#### find\_overlapping\_entries（重叠查找）

**用于**：ON 模式（自动句补全）

```python
def find_overlapping_entries(cache, file_path, sp, sy, ep, ey, is_dual):
    """
    在 sentence entries 中查找 sub 与查询范围 [sp,sy]→[ep,ey] 有重叠的条目。
    重叠判定用 _ranges_overlap，仅比较 page 和 y_pct（忽略 x_pct）。
    """
```

**重叠判定** (`_ranges_overlap`)：

```python
return not (e1 < s2 or e2 < s1)
# 即：两个区间有交集，比较维度为 (page, y_pct)
```

#### find\_containing\_entries（包含查找）

**用于**：OFF 模式（手动句模式）

```python
def find_containing_entries(cache, file_path, sp, sy, sx, ep, ey, ex, is_dual):
    """
    查找 first sub start ≤ query start 且 query end ≤ last sub end 的条目。
    即缓存条目"包含"查询范围。
    坐标优先级：page > y_pct > x_pct，使用 COORD_TOLERANCE(=0.001) 容差。
    """
```

#### coord\_le\_tolerant（容差坐标比较）

```python
COORD_TOLERANCE = 0.001  # 0.1% 页面尺寸

def coord_le_tolerant(ap, ay, ax, bp, by, bx):
    # 优先级：page > y_pct > x_pct
    # 当 page 相同且 y_pct 在容差范围内时，用 x_pct 决定
```

### 双栏逻辑坐标变换在缓存中的应用

**写入缓存前**（`reader_tab.py`）：

```python
if self._pdf_viewer.is_dual_column:
    transform_dual_column_coords(sub_sentences)
```

对每个 sub\_sentence 的坐标应用 `transform_dual_column_coords()`：

- 右栏：`x_pct -= 0.5`, `y_pct += 1.0`
- 左栏：不变

**缓存查找时**：查询坐标也需要经过同样的变换（由 `sentence_analyzer` 在拆分句子后立即执行），然后与缓存中已变换的坐标进行比较。

### 残句自整理 (\_fragment\_self\_merge)

**触发时机**：每次写入 sentence cache 后自动调用。

**位置**：`cache_store.py:find_mergeable_fragments()` (line 281-317)

**合并算法**：

```
循环直到没有可合并的 pair：
  遍历所有 sentence entries：
    如果条目被标记为 head_fragment：
      查找是否有另一个条目的最后一个 sub 的 end 坐标
      与该条目的第一个 sub 的 end 坐标相同（精确匹配，通过 _coord_eq）
      → 发现可合并对，方向为 "append"（另一个在前，该条目在后）

    如果条目被标记为 tail_fragment：
      查找是否有另一个条目的第一个 sub 的 start 坐标
      与该条目的最后一个 sub 的 start 坐标相同
      → 发现可合并对，方向为 "append"（该条目在前，另一个在后）

  调用 merge_entries() 合并发现的 pair
```

**`_coord_eq`** **精确匹配**：使用 `COORD_TOLERANCE = 0.001` (0.1% 页面尺寸) 比较 page、x\_pct 和 y\_pct。

```python
def _coord_eq(ap, ax, ay, bp, bx, by):
    return ap == bp and abs(ax - bx) < COORD_TOLERANCE and abs(ay - by) < COORD_TOLERANCE
```

**merge\_entries()**：

- 收集所有 sub\_sentences，按坐标排序
- 去重（相同 start 坐标的 sub 保留有非空 tgt 的版本）
- 重新计算 head\_fragment / tail\_fragment
- 删除旧条目，插入合并后的新条目

### 单栏/双栏独立分组

- **分组 key**：`"single"` vs `"dual"`
- **独立存储**：切换单/双栏时缓存完全隔离，不会交叉污染
- **切换确认**：`reader_tab._on_dual_column_toggle_requested()` 在切换时会弹窗提示"单栏与双栏 cache 相互独立"
- **合理性**：双栏模式下坐标经过了变换，与单栏模式的坐标空间不同，共用缓存会导致坐标误匹配

***

# 3.7 同行判断算法汇总

项目中存在多个"判断两个词是否同行"的位置，使用不同的算法和容差参数：

| 位置                                                  | 算法          | 容差/参数                                      | 适用场景           | 原因                            |
| --------------------------------------------------- | ----------- | ------------------------------------------ | -------------- | ----------------------------- |
| `_group_words_into_lines()` (pdf\_viewer:1227)      | 单点容差        | `LINE_TOLERANCE = 0.005` (0.5% 页高)         | 双栏重排、行分组（提取阶段） | 提取阶段词都来自同一页、同一字号，单点容差足够       |
| `_line_centers()` (pdf\_viewer:1192-1208)           | 单点容差聚类      | `LINE_TOLERANCE = 0.005`                   | 高亮渲染时的行识别      | 仅用于视觉渲染，与分组一致                 |
| `_same_line()` (pdf\_viewer:1385-1394)              | Y 区间重叠度     | `overlap >= 0.5 * min(height_a, height_b)` | 划词时的同行判断（锚点墙）  | 处理跨页、跨栏场景，不同字号/位置偏差的词需要更稳健的算法 |
| `_word_at_scene_pos` Step 5 (pdf\_viewer:1289-1296) | Y 区间重叠度     | `overlap >= 0.5 * min(height_a, height_b)` | 鼠标点击命中词的同行词收集  | 同上，不同字号词需要区间重叠判断              |
| `_draw_highlights()` (pdf\_viewer:1490-1539)        | 单点容差 + 按页分组 | `LINE_TOLERANCE = 0.005`                   | 绘制高亮矩形         | 渲染阶段按页分组后再判同行，单点容差可靠          |
| `_is_para_boundary()` (sentence\_analyzer:105-114)  | 间距比值        | `gap > median_line_gap * 1.1`              | 句补全时判断段落边界     | 动态计算中位行间距，自适应不同排版密度           |

### 为什么有两种算法

1. **单点容差**（LINE\_TOLERANCE）：适合**同页、同字号**的批量聚类场景。简单高效，对渲染和提取阶段足够。
2. **Y 区间重叠度**（\_same\_line）：适合**跨页、混合字号**的精确判断场景。例如：
   - 一个上标词（小字号，短区间）和一个正文词（大字号，长区间）可能有不同的 center\_y 但视觉上在同一行
   - 仅靠 center\_y 差距可能误判为不同行
   - 区间重叠度检查 Y 轴投影的重叠程度，更准确

### 容差参数的具体值

- `LINE_TOLERANCE = 0.005`：0.5% 页面高度。对于 A4 (297mm) 的 PDF，约为 1.5mm
- `SPATIAL_TOLERANCE = 0.0005`：0.05% 页面高度，约 0.15mm。用于空间过滤的软边界
- `COORD_TOLERANCE = 0.001`：0.1% 页面高度，约 0.3mm。用于缓存坐标精确匹配
- `BUFFER = 20`：20 个词，用于空间检索的缓冲窗口

***

# 4. 模块间耦合关系

## 数据流向图

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              main.py                                     │
│                                │                                         │
│                           MainApp                                        │
│                     (ui/main_app.py)                                     │
│                     ╱              ╲                                     │
│           PDFViewer                  ReaderTab                           │
│        (ui/pdf_viewer.py)         (ui/reader_tab.py)                     │
│           │      │                  │       │                            │
│           │      │    text_selected │       │                            │
│           │      └──────────────────┼───────┘                            │
│           │         auto_complete_  │                                    │
│           │         changed         │                                    │
│           │                        │                                    │
│     ┌─────┴─────┐          ┌───────┴────────┐                          │
│     │ 划词引擎   │          │  翻译调度       │                          │
│     │ _get_     │          │  on_pdf_       │                          │
│     │ selected_ │          │  selection()   │                          │
│     │ words()   │          │                │                          │
│     └─────┬─────┘          └───┬───┬────┬───┘                          │
│           │                    │   │    │                               │
│     ┌─────┴─────┐     ┌────────┴┐  │    │     ┌──────────────────┐     │
│     │ 隔离域     │     │ 句补全   │  │    │     │   LLM Client     │     │
│     │ _words_   │     │ expand_ │  │    │     │ (llm_client.py)  │     │
│     │ inside/   │     │ to_     │  │    │     └──────────────────┘     │
│     │ outside   │     │ sentence│  │    │                               │
│     └───────────┘     └────┬────┘  │    │                               │
│                            │       │    │                               │
│                     ┌──────┴───┐   │    │                               │
│                     │ 句子拆分  │   │    │                               │
│                     │ split_   │   │    │                               │
│                     │ sentences│   │    │                               │
│                     └──────┬───┘   │    │                               │
│                            │       │    │                               │
│                     ┌──────┴───────┴────┴──────┐                        │
│                     │       Cache Store        │                        │
│                     │   (cache_store.py)       │                        │
│                     │                          │                        │
│                     │  find_overlapping_entries│                        │
│                     │  find_containing_entries │                        │
│                     │  merge_entries           │                        │
│                     │  find_mergeable_fragments│                        │
│                     └──────────────────────────┘                        │
└──────────────────────────────────────────────────────────────────────────┘
```

### 关键信号流

```
pdf_viewer.text_selected(lo, hi, text)
  → main_app._on_pdf_selection()
    → reader_tab.on_pdf_selection()

pdf_viewer.context_menu_requested(text)
  → main_app._on_pdf_context_menu()
    → 弹出菜单 → reader_tab.on_context_menu_translate()

pdf_viewer.selection_started()
  → reader_tab._on_selection_started()  # 追加历史结果分隔线

pdf_viewer.auto_complete_changed(enabled)
  → main_app 持久化到 config.json

pdf_viewer.note_path_needed()
  → main_app._ensure_note_path()  # 自动生成 note 文件路径

reader_tab.inject_pdf_viewer(viewer)
  # 建立双向连接：reader_tab 持有 pdf_viewer 引用，
  # 同时连接 pdf_viewer 的 dual_column_toggle 和 isolate_path_needed 信号
```

### 隔离域 ↔ 划词引擎

```
隔离域变化 (_rebuild_word_lists)
  → 更新 _words_inside / _words_outside
    → 划词时 _snap_start 根据鼠标落点选择 _active_words
      → _word_at_scene_pos 使用 _active_words 作为候选池
        → _get_selected_words 使用 _active_words 作为源数据
```

隔离域不修改任何 word 对象，不影响缓存存储的坐标。

### 句补全 → 缓存写入

```
expand_to_sentence → 确定 head/tail fragment → split_sentences
  → transform_dual_column_coords（如果是双栏）
    → find_overlapping_entries / find_containing_entries
      → 如果有缓存命中 → 直接提取译文
      → 否则 → LLM 翻译 → 写入 sentence entry
        → _fragment_self_merge（自动整理残句）
```

### 公式回捞 → 几何重排 → emit 信号

```
用户鼠标拖拽 → _snap_start → _snap_end
  → _get_selected_words
    → 缓冲池扩展 (BUFFER=20)
    → 锚点墙过滤 (first_anchor / last_anchor)
    → 空间过滤 (SPATIAL_TOLERANCE)
    → 几何重排 (_group_words_into_lines → sort by x0_pct)
  → 拼接文本 → 清洗换行符
  → text_selected signal → reader_tab
```

***

# 5. 扩展接口分析

## 5.1 OCR 集成点

**数据提供源的位置**：`pdf_viewer.py:_extract_words()` (line 805-862)

当前数据流：

```
PyMuPDF (fitz) page.get_text("words") → _Word 对象列表
```

### 替换方式

在 `_extract_words()` 中，将 PyMuPDF 的 word 提取替换为 OCR 引擎的输出。核心替换点在：

```python
# pdf_viewer.py line 823
words_data = page.get_text("words")  # ← 替换这里

# 以及 line 813-821 的 span_info 提取
dict_data = page.get_text("dict")    # ← 替换这里（提供字号/粗体信息）
```

### 是否可以做到切换后不影响其他模块

**可以**，前提是 OCR 输出格式满足以下约定：

OCR 需要将输出转换为 `_Word` 对象列表，每个 `_Word` 包含：

- `page_idx`: 页码
- `x0_pct, y0_pct, x1_pct, y1_pct`: 百分比坐标（以页面尺寸归一化）
- `text`: 单词文本
- `size`: 字号（可选，默认 0.0）
- `flags`: 字体标记（可选，默认 0）
- `idx`: 全局索引（由 `_extract_words` 统一分配）

**不需要修改的模块**：

- 划词引擎（`_get_selected_words`）：只依赖 `_Word` 的坐标和文本字段
- 隔离域（`_rebuild_word_lists`）：只依赖 `_Word` 的 bbox 和 page\_idx
- 缓存系统：只依赖 sub\_sentence 的坐标和原文（由上层传入，不直接读取 `_Word`）
- 句补全（`expand_to_sentence`）：依赖 `_Word` 的 text、page\_idx、size、flags、y0\_pct

**需要提供的 OCR 数据格式**：

```
对于每个页面，OCR 需要输出：
[
  (x0, y0, x1, y1, text),  # bbox 以 PDF 点为单位的物理坐标
  ...
]
以及可选的每词字号和字体信息：
{
  (x0, y0): (size, flags),
  ...
}
```

**注意**：OCR 引擎需要能够将识别到的文本分割为"单词"粒度（以空格和标点分隔），因为整个项目的语义单元是 word 而非 character。

## 5.2 排版算法替换点

### 行分组算法 (`_group_words_into_lines`)

**位置**：`pdf_viewer.py:1210-1225`

**接口**：

```python
@staticmethod
def _group_words_into_lines(words: list[_Word]) -> list[list[_Word]]:
```

**输入**：`list[_Word]`（任意顺序）
**输出**：`list[list[_Word]]`（每行一个子列表，行内保持原始顺序）

**约定**：

- 每行内单词不需要排序（调用方自己排序）
- 行的顺序应从上到下
- 同一个 word 不应出现在多个行中

### 双栏识别算法 (`_reorder_dual_column`)

**位置**：`pdf_viewer.py:864-896`

**接口**：

```python
def _reorder_dual_column(page_words: list[_Word], page_width: float) -> list[_Word]:
```

**输入**：

- `page_words`: 单页的 word 列表（按 PDF 字符流原始顺序）
- `page_width`: 页面宽度（PDF points）

**输出**：重排后的 word 列表（阅读顺序）

**当前算法**：

- 调用 `_group_words_into_lines()` 分行
- 每行独立判断：是否有跨中线词（`x0_pct < 0.49 and x1_pct > 0.51`）
- 有跨中线词 → 整行保持原序（单栏行，如标题）
- 无跨中线词 → 左栏词在前，右栏词在后
- 左栏词 `center_x < 0.5`，右栏词 `center_x >= 0.5`

**替换约定**：新算法满足相同的输入输出接口即可，内部实现完全自由。更高级的算法可以利用：

- `_Word.size`（字号信息，标题通常字号更大）
- `_Word.flags`（粗体标记）
- 词间距的中位数
- 机器学习分类器

### 物理列分割 (`_split_line_by_physical_column`)

**位置**：`pdf_viewer.py:1231-1248`
**用途**：高亮渲染时，将一行的词按物理列拆分为左右两组，确保高亮矩形的绘制不会跨栏

## 5.3 其他可能的扩展点

### 支持更多语言

**当前的英文假设**：

- `_Word` 粒度是"单词"（以空格分隔），非字母
- `SENTENCE_ENDS = {'.', '。', '!', '？', '?'}` 的句末检测
- 空格拼接单词（`" ".join(w.text for w in words)`）

**扩展需要**：

1. **中文 PDF**：中文词间通常无空格，PyMuPDF 的 `get_text("words")` 可能返回词组或单字。需要调整拼接逻辑（中文不需要空格）
2. **日文**：类似中文，需要去除空格拼接
3. **RTL 语言（阿拉伯语、希伯来语）**：
   - 预览渲染：Qt 的 `QGraphicsScene` 不原生支持 RTL 文字方向
   - 句末检测需要添加 RTL 标点（`؟` 等）
   - `split_translation` 中的译文拆分逻辑使用 `。！？` 作为分隔符，对非中文输出需要调整

### 支持 RTL 文字

**主要修改点**：

1. **`_get_selected_words`** **几何重排** (line 1460)：RTL 阅读顺序从右到左，需要 `sort(key=lambda w: w.x0_pct, reverse=True)`
2. **`_word_at_scene_pos`**：不需要修改（空间命中算法是方向无关的）
3. **文本拼接**：RTL 语言不应用空格拼接，字符应直接连接
4. **高亮渲染**：不需要修改（基于 bbox 的几何渲染是方向无关的）

### 潜在的新隔离域模式

当前隔离域只有矩形区域。可以扩展为：

- 多边形域（支持更精确的图表区域圈选，但坐标包含判定会变得复杂）

由于隔离域完全封装在 `_rebuild_word_lists()` 中，所有新模式只需要修改该函数的过滤条件。

***

# 6. 已知设计取舍

## 6.1 百分比坐标系 vs 绝对坐标

**选择**：所有核心数据（`_Word`、zone、cache sub）使用百分比坐标。

**原因**：百分比坐标与渲染分辨率解耦。窗口 resize 时不需要重新提取 word 数据，只需要重新渲染页面 pixmap。高分辨率和低分辨率屏幕上，划词引擎的行为完全一致。

**代价**：每次坐标使用都需要转换（百分比 → scene 像素），有少量计算开销。但从架构简洁性看是值得的。

## 6.2 单词粒度 vs 字符粒度

**选择**：最小语义单元是 word（单词），不支持选中单词中的单个字母。

**原因**：

- PyMuPDF 的 `get_text("words")` 天然提供 word 粒度
- 翻译场景中 word 是自然的最小意义单元
- 字符粒度会让索引系统和缓存坐标系统复杂度暴涨

**限制**：不支持字母级别的精确选中（readme 中已注明）。

## 6.3 逻辑坐标变换的"使用时变换"策略

**选择**：双栏逻辑变换不在数据存储时执行，而在每次使用时通过 helper 函数计算。

**原因**：

- `_Word` 的物理坐标被多处使用（渲染、隔离域、鼠标命中），这些场景都需要物理坐标
- 只有划词选择和缓存匹配需要逻辑坐标
- 如果修改存储，需要额外标记"已变换"还是"未变换"，增加出错风险

**代价**：每次使用都需要判断 `is_dual_column` 并计算偏移，有微量性能开销。

## 6.4 缓存键 = PDF 绝对路径

**选择**：缓存以 PDF 文件的绝对路径作为 key。

**原因**：简单可靠，不需要额外的文件指纹或哈希。

**代价**：移动或重命名 PDF 后缓存失效，用户需要手动重新绑定（readme 中已注明）。

## 6.5 单栏/双栏缓存完全隔离

**选择**：`single` 和 `dual` 两组缓存，切换后独立使用。

**原因**：双栏模式下坐标经过了逻辑变换，与单栏模式的坐标空间不同。共用会导致坐标匹配错误。

**代价**：同一篇 PDF 在两种模式下需要分别翻译，切换后可能遇到"空缓存"。

## 6.6 残句自整理采用迭代合并

**选择**：`_fragment_self_merge()` 使用 while 循环反复查找可合并对，直到没有更多可合并项。

**原因**：合并操作可能产生新的可合并 fragment（例如合并 A+B 后的新条目可能又与 C 共享边界），一次性遍历无法处理这种级联合并。

**代价**：最坏情况下 O(n²) 的合并次数，但实践中缓存条目数通常不大（几百到几千）。

## 6.7 划词时 \_active\_words 在 press 时锁定

**选择**：`_active_words` 在鼠标按下时确定，整个拖拽过程中不改变。

**原因**：避免用户在 zone 边界附近拖拽时，`_active_words` 在 inside 和 outside 之间来回切换，导致选区内容不稳定。

**代价**：用户无法通过一次拖拽同时选中 zone 内和 zone 外的内容（这正是设计意图）。

## 6.8 缓存 OFF 时跳过所有缓存操作

**选择**：勾选"缓存 OFF"后，翻译不读取缓存也不写入缓存，但不影响自动句补全的行为。

**原因**：缓存错位是已知问题（readme 已有说明），"缓存 OFF"是用户的逃生舱。但句补全是独立的文本分析功能，不受缓存质量影响，因此不禁用。

## 6.9 仅测试过英文文献

**选择**：项目开发和测试的目标语言为英文原文 → 中文译文。

**原因**：作者的硕士论文为英文，翻译目标为中文。句末检测的标点集（`.`, `。`, `!`, `？`, `?`）和译文拆分逻辑都基于中文输出。

**限制**：其他语言对的翻译效果未经测试，句末检测可能不准确。

## 6.10 不支持的场景

- 扫描版 PDF（仅有图片，无可选中的文字层）
- 公式密集区域的回捞效果取决于 PDF 文字编码质量
- Windows 打包版（exe）未签名，可能触发 SmartScreen 警告

