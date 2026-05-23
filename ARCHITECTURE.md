# CY-Translator 架构文档

> 生成日期：2026-05-23

***

# 1. 项目概览

CY-Translator 是一个本地运行的 PDF 划词翻译工具。用户在 PDF 上划选英文文本，工具调用 OpenAI 兼容 API（DeepSeek、OpenAI、本地模型等）将选中内容翻译为中文。

***

# 2. 文件结构总览

## 入口

| 文件 | 职责 |
| --- | --- |
| `main.py` | 应用程序入口，设置 Qt 环境变量，启动 MainApp |

## UI 层 (`ui/`)

| 文件 | 职责 |
| --- | --- |
| `ui/main_app.py` | 顶层工作区：PDF 查看器 + 右侧面板的水平分割布局，管理 PDF 打开历史、per-PDF 配置文件对话框、信号路由 |
| `ui/pdf_viewer.py` | **核心模块**：PDF 渲染（懒加载 + 后台预渲染）、_Word 提取（通过 page_analysis 管线）、划词选择引擎、隔离域管理（框选/管理/扩散至全文）、笔记系统、TOC 目录面板、高亮渲染 |
| `ui/reader_tab.py` | **右侧控制面板**：两栏翻译模式（划词速翻 / 翻译持久化）、URL/Key/Model 管理、缓存查找与写入、句补全两阶段流程、LLM 调用调度、Prompt 文件管理 |

## 版面分析管线 (`page_analysis/`)

| 文件 | 职责 |
| --- | --- |
| `page_analysis/schema.py` | 数据契约定义：word 和 page 的标准 dict 格式，坐标归一化约定（底部原点，0–1 范围） |
| `page_analysis/pdf_parser.py` | PDF 解析入口：PyMuPDF 文本提取 + Tesseract OCR 回退 + 坐标归一化 |
| `page_analysis/xy_cut_sorter.py` | **XY-Cut++ 递归投影分割**：扫描线算法、邻居启发式孤立检测、动态阻塞清理、block_id 追踪 |
| `page_analysis/post_merge_indexer.py` | 后处理：噪声词块化、页码分组 Z-sort、全局索引注入 |
| `page_analysis/evaluate.py` | 版面分析评估工具 |
| `page_analysis/test_pdf_parser.py` | pdf_parser 单元测试 |
| `page_analysis/test_post_merge_indexer.py` | post_merge_indexer 单元测试 |

## 服务层 (`services/`)

| 文件 | 职责 |
| --- | --- |
| `services/cache_store.py` | 翻译缓存持久化：扁平 phrase/sentence 结构、idx 匹配、条目合并、残句自整理、format_version=3 |
| `services/sentence_analyzer.py` | 句子分析：句末检测、句补全扩展、句子切分、`<br>` 标记拼接与拆分、段落边界检测 |
| `services/note_store.py` | 笔记持久化：加载/保存笔记 JSON 文件 |
| `services/config_store.py` | 全局配置持久化：URL/Key/Model 管理、Prompt 文件列表、PDF 历史记录（含 per-PDF 配置路径） |
| `services/file_writer.py` | 翻译结果写文件：新建/追加 Markdown 文件 |
| `services/llm_client.py` | LLM API 客户端：通过 `/v1/chat/completions` 端点调用翻译 |
| `services/model_fetcher.py` | 模型列表获取：通过 `/v1/models` 端点拉取可用模型 |
| `services/prompt_loader.py` | 系统提示词加载：从 .txt 文件读取 prompt 内容 |

## 工具层 (`utils/`)

| 文件 | 职责 |
| --- | --- |
| `utils/paste_cleaner.py` | 粘贴文本清洗：去除 PDF 复制产生的多余换行符 |

## 配置与发布

| 文件 | 职责 |
| --- | --- |
| `config.json` | 全局配置文件（URL、Key、模型、提示词历史、PDF 历史、窗口尺寸、per-PDF 配置） |
| `prompt.txt` | 默认翻译提示词（纯文本输出） |
| `prompt_for_markdown.txt` | Markdown 格式翻译提示词 |
| `requirements.txt` | Python 依赖：PySide6、PyMuPDF、requests |
| `build_release.py` | PyInstaller 打包与发布脚本 |

***

# 3. 核心数据结构

## 3.1 _Word 对象

**定义位置**：`ui/pdf_viewer.py`，`@dataclass`

```python
@dataclass
class _Word:
    idx: int          # 全局阅读顺序索引（由 XY-Cut + 后处理分配）
    page_idx: int     # 所属页码（0-based）
    x0_pct: float     # 左边界百分比坐标 (0.0 ~ 1.0)，PDF 底部原点
    y0_pct: float     # 下边界百分比坐标 (0.0 ~ 1.0)
    x1_pct: float     # 右边界百分比坐标 (0.0 ~ 1.0)
    y1_pct: float     # 上边界百分比坐标 (0.0 ~ 1.0)
    text: str         # 单词文本
    size: float = 0.0  # 字号（来自 span 字典），0.0 表示未知
    flags: int = 0     # 字体标记，bit 4 (16) 表示粗体
    block_id: int = 0  # XY-Cut 叶子块 ID，用于高亮隔断和缓存键
```

**坐标系**：百分比坐标，以页面宽高为分母，PDF 标准底部原点（Y 轴向上）。`center_x` 和 `center_y` 是计算属性。

**idx 的语义**：`idx` 表示该词在整个文档中的全局阅读顺序。由 XY-Cut 递归投影分割确定块级顺序，再经页面内 Z-sort（上→下、左→右）和全局索引注入得到。**不再使用 PDF 原始字符流顺序**。

**block_id 的语义**：每个 XY-Cut 叶子块获得唯一递增的 `block_id`。同一块内的词属于同一视觉段落/列。用于高亮渲染时按块隔断（避免跨栏大矩形）。

**坐标存储**：_Word 始终存储物理百分比坐标（PDF 原始布局）。不再有双栏逻辑坐标变换——XY-Cut 已经在提取阶段按阅读顺序重排了词序。

**生命周期**：
1. `_extract_words()` 调用 `page_analysis` 管线创建所有 _Word 对象
2. 每次打开新 PDF 时重建
3. 若存在有效的布局缓存文件则从缓存加载（跳过重新解析）
4. 窗口 resize 不重建（百分比坐标与渲染分辨率无关）

## 3.2 _Zone（隔离域）

**存储**：`self._zones: list[dict]`，每个 dict 的坐标是物理百分比坐标。

```python
{
    "page": int,   # 页码（0-based）
    "x0": float,   # 左边界百分比 (0.0 ~ 1.0)
    "y0": float,   # 上边界百分比
    "x1": float,   # 右边界百分比
    "y1": float,   # 下边界百分比
}
```

**持久化**：JSON 数组，序列化到 `{pdf名}_isolate_{时间戳}.json` 文件。

**生命周期**：
1. 加载 PDF 时从 isolate 文件读入
2. 用户框选时追加
3. 管理模式下可拖动/调整大小/删除
4. 支持"扩散至全文"：将同一区域复制到所有页面（用于页眉、页脚、页码）
5. 每次变更后自动保存到文件

## 3.3 Cache 条目

**定义位置**：`services/cache_store.py`

**顶层结构（format_version=3）**：

```json
{
  "format_version": 3,
  "<pdf_absolute_path>": {
    "phrases": [...],
    "sentences": [...]
  }
}
```

**关键变化**：不再有 `single`/`dual` 分组——XY-Cut 自动处理所有排版，无需手动切换。结构完全扁平化。

### Phrase 条目

```json
{
  "src": "原文",
  "tgt": "译文"
}
```

按 `src` 精确字符串匹配，用于短词组（≤5 词且无句末标点）。

### Sentence 条目

```json
{
  "src": "完整原文（所有子句拼接）",
  "tgt": "完整译文（所有子句译文拼接）",
  "head_fragment": false,
  "tail_fragment": false,
  "sentences": [
    {
      "start_idx": 142,
      "end_idx": 156,
      "src": "子句原文",
      "tgt": "子句译文",
      "is_head_fragment": true,
      "is_tail_fragment": false
    }
  ]
}
```

**关键变化**：sub 条目不再存储坐标字段（`start_page`, `start_x_pct`, `start_y_pct`, `end_page`, `end_x_pct`, `end_y_pct`），改为存储 `start_idx` / `end_idx`（全局词索引）。这彻底消除了坐标容差问题和双栏/单栏坐标空间不一致问题。

## 3.4 Notes 条目

**定义位置**：`services/note_store.py`

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

## 3.5 布局缓存

**定义位置**：`pdf_viewer.py:_save_layout_cache()` / `_try_load_layout_cache()`

```json
[
  {
    "idx": 0,
    "page": 0,
    "x0": 0.12345678,
    "y0": 0.87654321,
    "x1": 0.14567890,
    "y1": 0.89012345,
    "text": "word",
    "size": 10.0,
    "flags": 0,
    "block_id": 3
  }
]
```

**用途**：首次解析 PDF 后，将完整的 `_words` 列表序列化保存。再次打开时，如果 PDF 修改时间未变，直接从布局缓存加载，跳过 XY-Cut 重新解析（节省 2–10 秒）。

***

# 4. 核心模块详细分析

## 4.1 PDF 解析管线

### 数据流

```
fitz.open(path) → parse_pdf() → 每页:
  ├─ _text_extract_words(): PyMuPDF get_text("words")
  │    → 原始词列表（top-left 坐标）
  ├─ 或 _ocr_extract_words(): Tesseract OCR 回退
  │    → 原始词列表（top-left 坐标）
  └─ _normalize_page():
       ├─ 坐标翻转：y0 = ph - y1, y1 = ph - y0（top-left → bottom-left）
       └─ 归一化：x /= pw, y /= ph → (0–1 范围)
           → 标准 page dict {page_number, width, height, words}
```

然后：

```
page dicts → CleanedXYCutSorter.sort() → 递归投影分割:
  ├─ 扫描线算法查找最佳切割线（V-cut / H-cut）
  ├─ 邻居启发式孤立检测：孤立词 → discarded_noise
  └─ 递归分割直到不可再分 → 每块内按 y↓ x→ 排序
       → 每个词获得 block_id

sorted words → pdf_viewer._extract_words():
  ├─ 转换 dict → _Word 对象
  ├─ 噪声词线性扫描插入正确位置
  ├─ 每块内同行按 x0_pct 精排
  └─ 分配全局 idx
```

### OCR 回退策略

`pdf_parser.py` 支持两阶段提取：
1. **文本提取优先**：`page.get_text("words")` 获取 PyMuPDF 原生文字层
2. **OCR 回退**：如果某页提取到的词数 < `text_min_words`（默认 5），自动调用 Tesseract OCR（通过 `page.get_textpage_ocr()`）

OCR 引擎可通过替换 `_ocr_extract_words()` 函数进行热插拔（详见 schema.py 的合约规范）。

### 坐标归一化

- PyMuPDF 原始坐标：top-left 原点，Y 轴向下
- 归一化后坐标：bottom-left 原点（PDF 标准），Y 轴向上，值域 [0, 1]
- 翻转公式：`new_y0 = 1.0 - old_y1`, `new_y1 = 1.0 - old_y0`

在 `pdf_viewer._extract_words()` 中转换为 _Word 时再次翻转以适配 UI 坐标（顶部原点，Y 向下）：
```python
y0_pct = round(1.0 - wdict["y1"], 8)
y1_pct = round(1.0 - wdict["y0"], 8)
```

## 4.2 XY-Cut++ 递归投影分割

**位置**：`page_analysis/xy_cut_sorter.py`

### 算法概述

XY-Cut++ 是经典递归投影分割（Recursive XY-Cut）的增强版，核心改进：

1. **扫描线算法**（O(n log n)）：通过排序事件点 + 扫描线找最佳空白间隙，替代 O(n²) 候选扫描
2. **邻居启发式孤立检测**：识别周围无相邻元素的孤立词（页眉、页脚、页码），自动丢弃并在最终排序后回注
3. **动态阈值**：根据词宽/词高中位数自适应计算切割阈值
4. **block_id 追踪**：每个叶子块分配唯一递增 ID

### 工作流程

```
recursive_segment(objects):
  1. if len(objects) <= 1 → 分配 block_id，返回
  2. find_best_cuts() → 扫描线找最佳 V-cut 和 H-cut
  3. 如果存在干净切割（gap >= threshold）：
     - V-cut vs H-cut：选 gap 更大的
     - 递归分割两个子组
  4. 如果切割被 ≤2 个桥接词阻塞：
     - 检查桥接词是否孤立（is_isolated）
     - 全部孤立 → 丢弃桥接词，递归分割剩余词
  5. 无法分割 → 组内按 y↓ x→ 排序，分配 block_id，返回
```

### 孤立检测算法

`is_isolated(target, all_objects, h_thresh, v_thresh)`：

- 检查 target 在四个方向（上下左右）是否有邻居
- 邻居定义：投影重叠 + 间距 ≤ 阈值
- 如果邻居方向数 < 2 → 判定为孤立（丢弃）

此机制自动过滤页眉、页脚、页码等与正文排版空间隔离的元素。

### 归一化坐标阈值

| 参数 | 值 | 用途 |
|------|-----|------|
| `_NORM_V_GAP` | 0.010 (1.0% 页宽) | 垂直切割（列分割）最小间隙 |
| `_NORM_H_GAP` | 0.005 (0.5% 页高) | 水平切割（段分割）最小间隙 |
| `MAX_BRIDGE_COUNT` | 2 | 最多允许的桥接词数 |

## 4.3 划词引擎

### _word_at_scene_pos 的命中算法

**位置**：`pdf_viewer.py:_word_at_scene_pos()`

采用**点到矩形 bbox 最短距离**算法：

```
1. scene_pos → PDF 百分比坐标 (_scene_to_pdf)
2. 候选池 = _active_words，优先同页词
3. 对每个候选词，计算点到 bbox 的欧氏距离：
   - 点在 bbox 内 → dx=0, dy=0 → dist=0（绝对优先）
   - 点在 bbox 外 → 只计超出 x/y 范围的偏移分量
4. 返回距离最小的词的索引
5. 如果最小距离 > snap_radius → 返回 None（未命中）
```

**snap_radius**：动态计算 = 全 PDF 最大词的角-中心欧氏距离。确保鼠标只要在词的 bbox 上方就能吸附，而宽词的边缘不会被邻近的短词"劫走"。

### _get_selected_words 的流程

**位置**：`pdf_viewer.py:_get_selected_words()`

由于 XY-Cut 已将词按阅读顺序排列且 idx 即全局阅读顺序，划词选择变得简单直接：

```
1. 确定 lo = min(start_idx, end_idx), hi = max(start_idx, end_idx)
2. 从 _active_words 中取 src[lo:hi+1] 切片
3. 返回切片中的所有词
```

**关键简化**：不再需要锚点墙过滤、缓冲池扩展、几何重排——这些都由 XY-Cut 在提取阶段一劳永逸地解决了。`_get_selected_words` 现在只做索引切片，O(1) 复杂度。

### 行分组算法（_group_words_into_lines）

**位置**：`pdf_viewer.py:_group_words_into_lines()`

采用 **Y 区间重叠度算法**（替代旧版的单点容差）：

```
overlap = min(w.y1_pct, line_y1) - max(w.y0_pct, line_y0)
if overlap > 0:
    if overlap >= 0.5 * min(word_height, line_height):
        → 同一行
    else:
        → 新行
```

此算法正确处理混合字号场景（上下标、公式元素），因为两个词的 Y 区间只要有 ≥50% 的重叠即判定为同行，不依赖 center_y 的单点比较。

## 4.4 隔离域系统

### _rebuild_word_lists 的预计算

**判定条件**：一个词属于某个 zone 当且仅当其 bbox **完全被 zone 包含**（四个边界都在 zone 内部）。

```
word.page_idx == zone.page
AND word.x0_pct >= zone.x0 AND word.x1_pct <= zone.x1
AND word.y0_pct >= zone.y0 AND word.y1_pct <= zone.y1
```

**触发时机**：
- 加载 zones 后
- 每次 zone 增删改后
- `_extract_words()` 末尾

### 划词时 _active_words 的切换

在 `_snap_start()`（鼠标按下时）确定：
1. 阅读模式 + 有 zone → 判断鼠标落点是否在 zone 内
2. 在 zone 内 → `_active_words = _words_inside`
3. 在 zone 外 → `_active_words = _words_outside`
4. 其他模式 → `_active_words = _words`（全部词）

**锁定机制**：`_active_words` 在按下时确定，整个拖拽过程中不改变。一次划词不会跨越 zone 边界。

### 扩散至全文

管理模式下右键隔离域 → "扩散至全文"：将当前 zone 的坐标复制到 PDF 所有页面（去重）。适用于页眉、页脚、页码等固定位置的内容。

## 4.5 自动句补全

### expand_to_sentence 的触发条件

1. `auto_complete` 开关为 ON
2. 选中词数 > 5（由 `classify()` 判定）

### 句末检测

```python
SENTENCE_ENDS = {'.', '。', '!', '？', '?'}
```

去掉末尾空白后，最后一个字符是否为句末标点。

### 扩展扫描逻辑

**左扫描（head_fragment）**：
- 从 lo-1 向左扫描，限于当前页
- 遇到句末标点 → `new_lo = i+1`, `head_fragment = False`
- 遇到段落边界 → 停止扫描
- 扫描到页首仍未找到句末 → `head_fragment = True`

**右扫描（tail_fragment）**：
- 如果 hi 已是句末 → `tail_fragment = False`
- 否则从 hi+1 向右扫描，限于当前页
- 遇到句末标点 → `new_hi = i`, `tail_fragment = False`
- 遇到段落边界 → 停止扫描
- 扫描到页尾仍未找到 → `tail_fragment = True`

### 段落边界判定

```python
gap > median_line_gap * 1.1 → 段落边界
```

其中 `median_line_gap` 从选中范围的相邻词 Y 间距计算中位数。若为单行选区，则从词 bbox 中位高度估算：`char_h * 1.2`。

### 基线统计

从选中范围采集：
- `median_size`：有字号信息的词的中位字号
- `bold_ratio`：粗体词比例
- `median_line_gap`：相邻词的 Y 间距中位数
- `x0_median`：起始 X 坐标中位数

### ON 和 OFF 模式的差异

| 特性 | ON（自动句补全） | OFF（手动模式） |
| --- | --- | --- |
| 分类阈值 | 词数 > 5 → sentence | 含句末标点 → sentence |
| 选区扩展 | 自动扩展 lo/hi 到句边界 | 不扩展，保持用户划选范围 |
| 缓存查找 | `find_overlapping_entries`（idx 重叠） | `find_containing_entries`（idx 包含） |
| 残句标记 | 来自 expand_to_sentence | 直接判断 lo-1 和 hi 是否为句末 |

## 4.6 缓存系统

### 扁平结构

```
cache (format_version=3)
  └─ [file_path]
       ├─ phrases: [{src, tgt}, ...]
       └─ sentences: [{src, tgt, head_fragment, tail_fragment, sentences: [sub, ...]}, ...]
```

不再有 `single`/`dual` 分组。所有 PDF 的缓存结构统一。

### idx 匹配（替代坐标匹配）

#### find_overlapping_entries（重叠查找）

**用于**：ON 模式（自动句补全）

```python
def find_overlapping_entries(cache, file_path, start_idx, end_idx):
    # 在 sentence entries 中查找 sub 的 idx 范围与 [start_idx, end_idx] 有重叠的条目
    # 重叠判定：sub.start_idx <= end_idx AND sub.end_idx >= start_idx
```

#### find_containing_entries（包含查找）

**用于**：OFF 模式（手动句模式）

```python
def find_containing_entries(cache, file_path, start_idx, end_idx):
    # 查找 first sub start_idx <= start_idx AND end_idx <= last sub end_idx 的条目
```

**关键优势**：idx 是整数，匹配精确无歧义，不再需要 `COORD_TOLERANCE` 容差。

### 残句自整理 (_fragment_self_merge)

**触发时机**：每次写入 sentence cache 后自动调用。

**合并算法**：
```
循环直到没有可合并的 pair：
  遍历所有 sentence entries：
    如果条目是 head_fragment：
      查找是否有另一个条目的 last sub end_idx + 1 == 该条目的 first sub start_idx
      → 合并（另一个在前，该条目在后）
    如果条目是 tail_fragment：
      查找是否有另一个条目的 first sub start_idx == 该条目的 last sub end_idx + 1
      → 合并（该条目在前，另一个在后）
  调用 merge_entries() 合并
```

**merge_entries()**：
- 收集所有 sub_sentences，按 `start_idx` 排序
- 去重（相同 `start_idx` 的 sub 保留有非空 tgt 的版本）
- 重新计算 head_fragment / tail_fragment
- 删除旧条目，插入合并后的新条目

### 旧版缓存兼容

`format_version=2` 的缓存在加载时静默忽略（视为空缓存）。首次翻译会在新版格式下重新生成。

## 4.7 `<br>` 标记与句数对齐

### 问题

英文句子拆分（以 `.` 为界）与中文翻译的句子切分（以 `。` 为界）不一定一一对应。LLM 可能输出与输入不同数量的句子，导致 `split_translation` 无法正确分配译文。

### 解决方案

**`join_subs_for_llm()`**：在非标点截断处（两个 sub_sentence 之间，前一个不以句末标点结尾）插入 `<br>` 标记：

```
sub_1_text <br> sub_2_text. sub_3_text!
```

**`split_translation()`**：先按 `<br>` 切分，再按中文标点（`。！？`）切分每段，最后按 `expected_count` 合并多余部分或填充空串。

**前台显示**：`_show_result()` 在显示前过滤所有 `<br>` 标记。

***

# 5. UI 架构

## 5.1 顶层布局

```
MainApp (QVBoxLayout)
├─ Top Bar: 窗口尺寸选择 + 打开 PDF 按钮 + 文件名标签
└─ QSplitter
   ├─ Left Area (QHBoxLayout)
   │   ├─ TOCPanel (可折叠目录侧栏，200px/15px)
   │   └─ PDFViewer (PDF 渲染 + 交互)
   └─ ReaderTab (右侧控制面板，固定 ≥500px)
        ├─ 模型配置（URL/Key/Model/Prompt 全局共享）
        └─ QTabWidget
            ├─ Tab "划词速翻"（Fast）
            └─ Tab "翻译持久化"（Persist）
```

## 5.2 信号流

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
  → main_app._on_auto_complete_changed()  # 持久化到 config.json

pdf_viewer.note_path_needed()
  → main_app._ensure_note_path()  # 自动生成 note 文件路径

pdf_viewer.layout_path_needed()
  → main_app._ensure_layout_path()  # 自动生成 layout 缓存路径

pdf_viewer.toc_collapsed_changed(collapsed)
  → main_app._on_toc_collapsed_changed()  # 持久化

pdf_viewer.isolate_path_needed()
  → reader_tab._ensure_per_pdf_paths()  # 自动生成 isolate 文件路径

reader_tab.inject_pdf_viewer(viewer)
  # 建立双向连接，关联 isolate_path_needed 信号
```

## 5.3 PDF 渲染

### 懒加载策略

1. **首次加载**：计算所有页面的场景位置（placeholder 灰色矩形），同步渲染当前页 ±2 页
2. **后台预渲染**：剩余页面按距当前页的距离排序，在 `_PrerenderWorker` 中后台渲染，每 2 页一批，批次间间隔 500ms
3. **滚动画布**：`_on_scrolled()` 检测当前可见范围，按需同步渲染 ±3 页内未渲染的页面
4. **窗口 resize**：不重新提取 word 数据（百分比坐标不受影响），仅重新渲染页面 pixmap（100ms 防抖）

### 坐标映射

| 坐标系 | 用途 |
| --- | --- |
| PDF 物理坐标（points） | `_page_width_pts`, `_page_height_pts`，页面原始尺寸 |
| 百分比坐标（0–1） | `_Word` 的 `x0_pct/y0_pct/x1_pct/y1_pct`，zone 坐标 |
| Scene 坐标（像素） | QGraphicsScene 中的实际渲染位置 |

转换公式：
```python
# 百分比 → scene
scene_x = x_pct * available_width
scene_y = page_offsets[page_idx] + y_pct * page_height_pts[page_idx] * scale_factor

# scene → 百分比
x_pct = scene_x / available_width
y_pct = (scene_y - page_offsets[page_idx]) / (page_height_pts[page_idx] * scale_factor)
```

## 5.4 高亮渲染

`_draw_highlights()` 在同行内按 `block_id` 分组绘制独立矩形：

```
for each line in selected words:
  group words by block_id
  for each block_group:
    draw single rect spanning the block's words only
```

这确保跨栏选区不会产生从左栏延伸到右栏的大矩形，每个栏/块有独立的高亮矩形。

## 5.5 笔记系统

- **放置**：笔记模式（按钮或 Alt+N）下点击 → 在 scene 位置放置笔记图标
- **编辑**：点击图标 → 展开 `_NoteEditor`（QTextEdit）通过 `_NoteProxy`（支持右下角边缘拖拽 resize）
- **拖拽**：笔记模式下拖拽图标改变位置
- **保存**：500ms 防抖自动保存 + 关闭时强制保存
- **删除**：右键图标 → 删除笔记（不可撤销）

## 5.6 Per-PDF 配置

每个 PDF 有独立的四个配置文件路径：
- **缓存文件** (`_cache`)：翻译缓存
- **隔离文件** (`_isolate`)：隔离域配置
- **版面文件** (`_layout`)：布局缓存（词列表）
- **笔记文件** (`_note`)：笔记数据

默认自动生成在 `data/` 目录，可在 PDF 历史界面的 `...` 配置按钮中手动绑定。

## 5.7 翻译模式

### 划词速翻（Fast）

划选 PDF 文本 → 自动（或右键触发）翻译，结果显示在右侧文本框。支持：
- 自动句补全 ON/OFF
- 缓存 OFF 选项
- 短语缓存（精确匹配）/ 句子缓存（idx 重叠匹配）
- 两阶段流程（扩展 → 缓存检查 → LLM 翻译）

### 翻译持久化（Persist）

手动输入或粘贴文本 → 翻译 → 写入 Markdown 文件：
- **New**：创建新 .md 文件，指定目录和文件名
- **Append**：追加到已有 .md 文件
- 支持粘贴时自动去换行 / 手动清洗换行

### 工作线程

所有 LLM 调用在 `QThread` 子类中执行：
- `_FastTranslateWorker`：划词速翻
- `_PersistTranslateWorker`：翻译持久化（含文件写入）

通过信号 `finished` 回传结果，不阻塞 UI。

## 5.8 删除缓存条目

### 快速删除（"删除缓存"按钮）

每次翻译完成后解封右侧面板的"删除缓存"按钮。按钮通过 `_last_cache_key` 定位当前显示结果对应的缓存条目：

- **phrase 条目**：按 `src` 字段精确字符串匹配
- **sentence 条目**：按 `start_idx` / `end_idx` 范围精确包含匹配（`entry.subs[0].start_idx <= key.start_idx and key.end_idx <= entry.subs[-1].end_idx`）

删除后立即 `save_cache()` 持久化，并在显示区追加 `[缓存已删除，下次将重新翻译]` 提示。

### 句级管理（"管理条目"二级对话框）

`_CacheManageDialog`（一级）新增"管理条目"按钮，选中缓存文件后可用。点击打开 `_EntryManageDialog`（二级）：

- 仅显示 **sentence** 条目（不显示 phrase 条目），按 `start_idx` 升序排列
- 每条显示原文/译文（超过 120 字截断，tooltip 显示完整内容）+ "删除"按钮
- 点击删除：通过 `start_idx` 定位缓存条目并从文件删除即时保存，列表即时重建刷新（无确认弹窗）
- 关闭二级对话框后，一级页面重新读取缓存文件更新条目统计数字

### 与翻译历史的交互

点击"删除缓存"后，当前翻译历史条目被标记为 `deleted=True`（不立即移除）。下一次划词选择时（`on_pdf_selection` 入口），`_purge_deleted_entries()` 扫描并清除所有标记为 deleted 的历史条目及其显示控件。

## 5.9 半持久翻译历史

### 数据模型

```python
self._history: list[dict] = []
# 每项结构：{"tgt": str, "start_idx": int, "end_idx": int, "timestamp": "14:23:05", "deleted": bool}
```

- 上限 500 条，超出时从头部删除最旧的 100 条
- 仅存于内存，关闭应用即释放，不写入任何文件
- 切换 PDF 时自动清空

### 显示区

翻译结果区由 `QTextEdit` 改为 `QScrollArea` + 控件列表：

- **追加模式**：每条新翻译追加到列表底部，不再清空旧内容
- **记录格式**：分隔线（`─`×60）+ 时间戳 + 译文（`_ClickableLabel`，可选文本）
- **分隔线逻辑**：每次新划词（`on_pdf_selection`）插入"以上为历史消息"分隔行；翻译结果返回后删除分隔行并追加新记录

### 虚拟滚动

- 显示区最多同时渲染 100 条记录的 widget
- **追加时**：超过 100 条则删除顶部 50 条旧 widget，`_render_start += 50`
- **向上滚动**：scrollbar 到达顶端时从 `_history` 前段取 50 条插入顶部，同时删除底部 50 条，通过 `sizeHint` 累计高度保持滚动位置不跳动
- **回到底部按钮**：若最新记录未渲染则清空显示区从末尾重新取 100 条渲染并滚动到底

### 导航跳转

点击/右键历史条目 → `pdf_viewer.navigate_to_range(start_idx, end_idx)`：

1. 调用 `set_highlight_range` 设置高亮
2. 计算 `start_idx` 对应词的 scene Y 坐标
3. 设置 `verticalScrollBar().setValue(word_scene_y - viewport_height / 2)`，使该词位于可视区域垂直中心

### 横向滚动禁止

历史显示区设置 `setHorizontalScrollBarPolicy(ScrollBarAlwaysOff)`，译文 `QLabel` 开启 `setWordWrap(True)` 确保自动折行。

***

# 6. 模块间耦合关系

## 数据流向图

```
main.py → MainApp
              ├─ PDFViewer
              │    ├─ page_analysis/pdf_parser.py  → 提取 + 归一化
              │    ├─ page_analysis/xy_cut_sorter.py → XY-Cut++ 排序
              │    ├─ page_analysis/post_merge_indexer.py → 后处理
              │    ├─ 划词引擎 (_get_selected_words)
              │    ├─ 隔离域系统 (_zones, _words_inside/outside)
              │    ├─ 笔记系统 (_notes, _NoteProxy)
              │    ├─ TOC 面板
              │    └─ 懒加载渲染 (_PrerenderWorker)
              │
              └─ ReaderTab
                   ├─ services/cache_store.py → 缓存读写
                   ├─ services/sentence_analyzer.py → 句补全 + 拆分
                   ├─ services/llm_client.py → LLM API 调用
                   ├─ services/model_fetcher.py → 模型列表获取
                   ├─ services/prompt_loader.py → Prompt 加载
                   ├─ services/file_writer.py → 翻译结果持久化
                   ├─ services/config_store.py → 全局配置
                   └─ services/note_store.py → 笔记持久化
```

## 隔离域 ↔ 划词引擎

```
隔离域变化 (_rebuild_word_lists)
  → 更新 _words_inside / _words_outside
    → 划词时 _snap_start 根据鼠标落点选择 _active_words
      → _word_at_scene_pos 使用 _active_words 作为候选池
        → _get_selected_words 直接从 _active_words 切片
```

## 句补全 → 缓存写入

```
expand_to_sentence → 确定 head/tail fragment → split_sentences
  → join_subs_for_llm（插入 <br> 标记）
    → find_overlapping_entries / find_containing_entries（按 idx）
      → 缓存命中 → 直接提取译文
      → 缓存未命中 → LLM 翻译 → split_translation（按 <br> + 标点切分）
        → 写入 sentence entry
          → _fragment_self_merge（自动整理残句）
```

## 划词 → 翻译

```
用户鼠标拖拽 → _snap_start → _snap_end
  → _get_selected_words (idx 切片)
  → 拼接文本 → clean_newlines
  → text_selected signal → reader_tab.on_pdf_selection()
    → classify() 判断 phrase/sentence
    → 缓存查找 / LLM 翻译
```

***

# 7. 设计决策

## 7.1 XY-Cut 排版引擎 vs 启发式双栏检测

**选择**：XY-Cut++ 递归投影分割自动处理所有排版。

**原因**：自动识别多栏、表格、参考文献等复杂排版，词序由算法保证，无需用户手动切换单/双栏。扫描线算法性能为 O(n log n)。

**代价**：首次解析 PDF 需要 2–10 秒（通过布局缓存缓解）。排版极端复杂的 PDF 可能不完美。

## 7.2 idx 匹配 vs 坐标匹配

**选择**：缓存以全局词索引（`start_idx` / `end_idx`）匹配，而非百分比坐标。

**原因**：
- idx 是整数，匹配精确无歧义
- 不受坐标系变换影响
- 不受坐标容差问题影响（旧版 `COORD_TOLERANCE=0.001` 在某些场景下不够精确）
- 缓存条目在不同单/双栏模式间自然统一

**代价**：移除双栏逻辑后，旧版缓存（format_version=2）全部失效。

## 7.3 百分比坐标系 vs 绝对坐标

**选择**：所有核心数据使用百分比坐标（0–1 范围，以页面尺寸归一化）。

**原因**：与渲染分辨率解耦。窗口 resize 时不需要重新提取 word 数据。

**代价**：每次坐标使用都需要转换（百分比 → scene 像素），有微量计算开销。

## 7.4 单词粒度 vs 字符粒度

**选择**：最小语义单元是 word（单词）。

**原因**：PyMuPDF 天然提供 word 粒度；翻译场景中 word 是自然的最小意义单元。

**限制**：不支持字母级别的精确选中。

## 7.5 缓存键 = PDF 绝对路径

**选择**：缓存以 PDF 文件的绝对路径作为 key。

**代价**：移动或重命名 PDF 后缓存失效，需要手动重新绑定。

## 7.6 划词时 _active_words 在 press 时锁定

**选择**：`_active_words` 在鼠标按下时确定，整个拖拽过程中不改变。

**原因**：避免用户在 zone 边界附近拖拽时，候选池在 inside/outside 之间来回切换。

## 7.7 布局缓存

**选择**：首次解析后序列化词列表，再次打开时直接加载（前提：PDF 修改时间未变）。

**原因**：XY-Cut 解析大型 PDF（200+ 页）可能需要数秒，布局缓存消除重复等待。

**代价**：PDF 内容变化（如 OCR 层更新）而修改时间未变时，布局缓存可能过期。此时需手动删除布局文件。

## 7.8 点到 bbox 距离的划词命中

**选择**：`_word_at_scene_pos` 使用点到矩形 bbox 的最短欧氏距离。

**原因**：解决了长词边缘被上方短词"劫走"的问题。点在 bbox 内时距离为 0（绝对优先），在 bbox 外时只计算超出 x/y 范围的偏移分量。

## 7.9 Y 区间重叠的行分组

**选择**：`_group_words_into_lines` 使用 Y 区间重叠度（≥50% 即同行）。

**原因**：正确处理混合字号场景（上下标、公式元素）。仅靠 center_y 单点容差在字号差异大时会误判不同行。

## 7.10 仅测试过英文文献

**选择**：目标语言为英文原文 → 中文译文。

**限制**：句末检测标点集基于英文/中文。其他语言对未经测试。

***

# 8. 扩展接口

## 8.1 OCR 引擎替换

**位置**：`page_analysis/pdf_parser.py:_ocr_extract_words()`

替换此函数的实现即可切换 OCR 后端（当前基于 PyMuPDF 内置 Tesseract）。函数签名：

```python
def _ocr_extract_words(page: fitz.Page, language: str, dpi: int) -> Optional[List[dict]]
```

返回的每个 dict 需包含 `x0, y0, x1, y1, text`（PyMuPDF top-left 坐标）。坐标归一化由 `_normalize_page()` 统一处理。

## 8.2 PDF 解析器替换

**位置**：`page_analysis/schema.py` 定义合约

替换整个 `parse_pdf()` 函数即可。签名：

```python
def parse_pdf(filepath: str, **kwargs) -> List[dict]
```

返回的 page dict 需符合 `REQUIRED_PAGE_KEYS`，每页的 word dict 需符合 `REQUIRED_WORD_KEYS`。坐标需为归一化坐标（0–1，底部原点）。

## 8.3 排版算法替换

`CleanedXYCutSorter.sort()` 接受 `list[dict]`（标准 word dict），返回排序后的 `list[dict]`。替换此函数即可接入不同的排版算法，下游（`_extract_words`）不感知算法细节。

## 8.4 支持更多语言

主要修改点：
- **中文/日文 PDF**：word 粒度可能需要调整（中文无空格分隔），拼接逻辑需修改
- **RTL 语言**：`_Word.text` 拼接需去除空格；Qt 不原生支持 RTL，渲染方向可能需要额外处理
- **句末检测**：需添加目标语言的句末标点

***

# 9. 已知限制

- 扫描版（图片型）PDF 暂不支持（需 PDF 内有可选中文字层，OCR 回退可部分缓解但效果有限）
- 最小语义单元是 word（单词），无法选中单词内的单个字母
- 仅测试过英文文献 → 中文翻译场景
- Windows exe 未签名，可能触发 SmartScreen 警告
- 句子匹配依赖标点符号切分，在标点使用不规范的 PDF 中可能产生错误的句子划分
- XY-Cut 在极端复杂排版（如多栏嵌套表格）中可能产生非最优排序

***

# 10. 依赖

```
PySide6 >= 6.4.0    # Qt for Python UI 框架
PyMuPDF >= 1.22.0    # PDF 渲染 + 文字提取 + OCR
requests >= 2.28.0   # HTTP 客户端（LLM API 调用）
```

可选依赖：
- Tesseract OCR（系统级安装，PyMuPDF 通过 `get_textpage_ocr()` 调用）
