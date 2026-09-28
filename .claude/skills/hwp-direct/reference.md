# OWPML (HWPX XML) reference for raw edits

Read this before touching XML through `doc.raw`, `doc.derive`, `doc.modify_*`. Everything here was checked
against files written by Hancom Office.

## Namespaces and units

| Prefix | Constant | URI | Used for |
|---|---|---|---|
| `hp:` | `HP` | `http://www.hancom.co.kr/hwpml/2011/paragraph` | body: p, run, t, ctrl, tbl, pic, secPr |
| `hh:` | `HH` | `http://www.hancom.co.kr/hwpml/2011/head` | header definitions: charPr, paraPr, borderFill, tabPr, style |
| `hc:` | `HC` | `http://www.hancom.co.kr/hwpml/2011/core` | shared bits: fillBrush, margins inside paraPr, img |

In lxml use Clark notation: `el.find(f"{HH}spacing")`, `el.iter(f"{HP}t")`.

**HWPUNIT**: 1 mm = 283.465, 1 pt = 100, A4 width = 59528.

**The doubling rule.** Some header values are stored twice inside `<hp:switch>`: an `<hp:case hp:required-namespace="…/HwpUnitChar">` branch with the real value and an `<hp:default>` branch with **2 × that value**. This applies to paragraph margins (`hc:intent/left/right/prev/next`), non-percent line spacing (`FIXED`, `AT_LEAST`, `BETWEEN_LINES`) and tab positions. Percent line spacing is the same in both branches. When you edit one branch, edit both.

## Body structure (Contents/section0.xml ...)

```xml
<hp:p paraPrIDRef="3" styleIDRef="0">          <!-- paragraph: format = paraPr 3, style 0 -->
  <hp:run charPrIDRef="7">                       <!-- run: character format charPr 7 -->
    <hp:t>텍스트<hp:tab width="4000" leader="0" type="1"/>다음<hp:lineBreak/>줄</hp:t>
  </hp:run>
  <hp:run charPrIDRef="0"><hp:ctrl>...</hp:ctrl></hp:run>   <!-- controls: footNote, fieldBegin, bookmark, colPr -->
  <hp:run charPrIDRef="0"><hp:tbl ...>...</hp:tbl></hp:run> <!-- objects live inside runs -->
  <hp:linesegarray>...</hp:linesegarray>        <!-- layout cache; doc.touch(p) removes it after edits -->
</hp:p>
```
- The first paragraph of every section holds `<hp:secPr>` (page setup) and `<hp:ctrl><hp:colPr/>` in its first run. Never delete or move them; `delete_paragraphs` handles this for you.
- Inline `<hp:tab>` attributes are a layout cache: `type` 1=LEFT 2=RIGHT 3=CENTER 4=DECIMAL, `leader` = index in NONE, SOLID, DOT, DASH, DASH_DOT, DASH_DOT_DOT, LONG_DASH, CIRCLE, DOUBLE_SLIM, SLIM_THICK, THICK_SLIM, SLIM_THICK_SLIM.

### Table
```xml
<hp:tbl rowCnt="3" colCnt="2" borderFillIDRef="3" repeatHeader="1">
  <hp:sz width="42520" height="..."/><hp:pos treatAsChar="1" horzAlign="CENTER" .../>
  <hp:tr>
    <hp:tc borderFillIDRef="5" header="1" hasMargin="0">
      <hp:subList vertAlign="CENTER" textDirection="HORIZONTAL"><hp:p>...</hp:p></hp:subList>
      <hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/>
      <hp:cellSz width="21260" height="282"/><hp:cellMargin left="510" right="510" top="141" bottom="141"/>
    </hp:tc>
  </hp:tr>
</hp:tbl>
```
Merged cells: only the top-left `tc` exists, with `cellSpan`; covered positions have no `tc`. Use the API for structure changes (it re-validates the grid).

### Picture / shape position (`hp:pos`)
`treatAsChar="1"` = inline like a character. Floating: `treatAsChar="0"`, `vertRelTo`/`horzRelTo` = PAPER | PAGE | PARA (horz also COLUMN), `vertOffset`/`horzOffset` in HWPUNIT, `vertAlign` TOP/CENTER/BOTTOM, `horzAlign` LEFT/CENTER/RIGHT; the object's `textWrap` = SQUARE | TOP_AND_BOTTOM | BEHIND_TEXT | IN_FRONT_OF_TEXT. `doc.float_image` / `add_textbox(floating=...)` set all of this.

### Section page setup (`hp:secPr`)
```xml
<hp:pagePr landscape="WIDELY" width="59528" height="84186">   <!-- NARROWLY = landscape -->
  <hp:margin left="8504" right="8504" top="5668" bottom="4252" header="4252" footer="4252" gutter="0"/>
</hp:pagePr>
```
Prefer `doc.page_setup(...)`.

## Header definitions (Contents/header.xml) - shared, clone before changing

### charPr (글자 모양) - `doc.derive("charPr", id, fn)` / `doc.modify_char_pr(t, fn, match=...)`
```xml
<hh:charPr id="0" height="1000" textColor="#000000" shadeColor="none" borderFillIDRef="2">
  <hh:fontRef hangul="1" latin="1" .../>        <!-- font ids per script; use format_text(font=...) to add fonts -->
  <hh:ratio hangul="100" .../>                    <!-- 장평 % -->
  <hh:spacing hangul="0" .../>                    <!-- 자간 % -->
  <hh:relSz hangul="100" .../>  <hh:offset hangul="0" .../>   <!-- relative size %, baseline offset % -->
  <hh:bold/> <hh:italic/>                         <!-- present = on -->
  <hh:underline type="NONE|BOTTOM|CENTER|TOP" shape="SOLID" color="#000000"/>
  <hh:strikeout shape="NONE|SOLID|..." color="#000000"/>
  <hh:outline type="NONE"/>  <hh:shadow type="NONE|DROP" color="#C0C0C0" offsetX="10" offsetY="10"/>
  <hh:supscript/> or <hh:subscript/>
</hh:charPr>
```
`height` = size × 100. Each of the 7 script attributes (hangul, latin, hanja, japanese, other, symbol, user) should be set together.

### paraPr (문단 모양) - `doc.modify_para_pr(t, fn)`
```xml
<hh:paraPr id="0" tabPrIDRef="0" condense="0" snapToGrid="1">
  <hh:align horizontal="JUSTIFY|LEFT|RIGHT|CENTER|DISTRIBUTE" vertical="BASELINE"/>
  <hh:heading type="NONE|OUTLINE|NUMBER|BULLET" idRef="0" level="0"/>
  <hh:breakSetting keepWithNext="0" keepLines="0" pageBreakBefore="0" widowOrphan="0" .../>
  <hp:switch>
    <hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/HwpUnitChar">
      <hh:margin><hc:intent value="0" unit="HWPUNIT"/><hc:left .../><hc:right .../><hc:prev .../><hc:next .../></hh:margin>
      <hh:lineSpacing type="PERCENT|FIXED|AT_LEAST|BETWEEN_LINES" value="160" unit="HWPUNIT"/>
    </hp:case>
    <hp:default> ...same elements, absolute values ×2... </hp:default>
  </hp:switch>
  <hh:border borderFillIDRef="2" offsetLeft="0" offsetRight="0" offsetTop="0" offsetBottom="0" connect="0"/>
</hh:paraPr>
```
`intent` = first-line indent (negative = hanging), `prev`/`next` = space before/after.

### borderFill (테두리/배경) - `doc.modify_cells(c, border_fill=fn)`
```xml
<hh:borderFill id="3" threeD="0" shadow="0" centerLine="NONE">
  <hh:slash type="NONE|CENTER" .../><hh:backSlash type="NONE|CENTER" .../>
  <hh:leftBorder type="SOLID" width="0.12 mm" color="#000000"/>   <!-- right/top/bottom likewise -->
  <hh:diagonal type="SOLID" width="0.1 mm" color="#000000"/>
  <hc:fillBrush><hc:winBrush faceColor="#D9E2F3" hatchColor="#999999" alpha="0"/></hc:fillBrush>
</hh:borderFill>
```
Widths must be one of: 0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 1.0, 1.5, 2.0, 3.0, 4.0, 5.0 mm (written `"0.4 mm"`). Child order matters: slash, backSlash, left, right, top, bottom, diagonal, fillBrush.

## Example: vertical center + 3 mm top padding on a cell range, raw
```python
from hwp_mcp.api import HwpDoc, HP
doc = HwpDoc.open(path)
def cell(tc):
    tc.find(f"{HP}subList").set("vertAlign", "CENTER")
    tc.set("hasMargin", "1")
    tc.find(f"{HP}cellMargin").set("top", str(round(3 * 283.465)))
doc.modify_cells("t0.r1-3.c*", cell=cell)
doc.save()
```
