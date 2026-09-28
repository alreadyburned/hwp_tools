# hwp_tools — AI용 한글 문서 편집 VS Code 확장

Claude Code, VS Code Copilot(에이전트 모드) 같은 AI가 **한글 문서(.hwpx / .hwp)를 직접 만들고 고칠 수 있게** 해 주는 VS Code 확장입니다.
한컴오피스 없이 동작합니다(순수 Python, [python-hwpx](https://github.com/airmang/python-hwpx) 기반).

## 구성

| 구성 요소 | 역할 |
|---|---|
| MCP 서버 (`server/hwp_mcp`) | 28개 `hwp_*` 도구. 모든 편집은 스키마·한컴 열기 안전성 검증 후에만 저장, 실패 시 파일 변경 없음 |
| 직접 실행 API (`server/hwp_mcp/api.py`) | AI가 Python 스크립트로 문서를 편집. MCP 도구 전부 + 고급 기능 + 안전한 XML 직접 편집. 마지막에 한 번 검증·저장 |
| 검증 명령 (`python -m hwp_mcp.check`) | 파일 검증, 문단 주소·서식 목록, 근사 미리보기 PNG |
| 스킬 (`skill/hwp-direct`) | Claude Code 스킬: 언제 MCP/스크립트를 쓰는지, 반드시 지킬 규칙, 검증된 레시피, OWPML XML 참조 |
| VS Code 확장 (`extension.js`) | `~/.hwp-mcp/venv` Python 환경 자동 구성, VS Code MCP 등록, Claude Code용 `.mcp.json`·스킬 설치, `.hwpx/.hwp` 근사 미리보기 |

## 설치

1. `python scripts/build_vsix.py` → `dist/hwp-tools-<버전>.vsix`
2. `code --install-extension dist/hwp-tools-<버전>.vsix` (확장 ID `local.hwp-tools`; VS Code 확장 ID에는 `_`를 쓸 수 없어 `-` 사용)
3. VS Code 재시작 → 첫 실행 시 Python 환경을 자동 설치(인터넷 필요, 1회)
4. Claude Code에서 쓰려면: 명령 팔레트 → **hwp_tools: Claude Code에 HWP MCP 서버·스킬 등록** → Claude Code 재시작 → `/mcp`로 `hwp` 확인
   (워크스페이스에 `.mcp.json`과 `.claude/skills/hwp-direct/`가 생깁니다. 이 저장소의 `.claude/skills/hwp-direct`는 `skill/hwp-direct`를 설치한 사본입니다.)

요구 사항: Python 3.10+, VS Code 1.101+.

## 도구 (28개)

| 분류 | 도구 |
|---|---|
| 문서 | `hwp_create_document`, `hwp_read_document`, `hwp_get_paragraph`, `hwp_find_text`, `hwp_save_as`, `hwp_undo` |
| 텍스트 | `hwp_insert_paragraph`, `hwp_set_paragraph_text`, `hwp_replace_text`, `hwp_delete_paragraphs` |
| 서식 | `hwp_format_text`(글자 모양: 글꼴·크기·굵게·기울임·밑줄·취소선·색·형광·위/아래첨자·장평·자간, 부분 범위 지정), `hwp_set_paragraph_format`(정렬·줄간격·문단 위/아래 간격·여백·들여쓰기/내어쓰기·쪽 나눔), `hwp_list_styles`, `hwp_apply_style`, `hwp_create_style`, `hwp_set_list`(글머리표/문단 번호) |
| 표 | `hwp_insert_table`, `hwp_get_table`, `hwp_set_cell_text`, `hwp_merge_cells`, `hwp_split_cell`, `hwp_format_cells`(배경·테두리 종류/굵기/색·바깥/안쪽 선·세로 정렬·안 여백), `hwp_table_structure`(행/열 삽입·삭제, 표 삭제), `hwp_set_table_layout`(열 너비·행 높이·표 정렬·제목 행 반복) |
| 그림 | `hwp_insert_image`(본문/셀 안, 비율 유지), `hwp_edit_image`(크기 변경·삭제) |
| 쪽 | `hwp_page_setup`(용지·방향·여백·다단), `hwp_set_header_footer`(머리말/꼬리말·쪽 번호) |

### AI가 헷갈리지 않도록 한 설계

- **상태 없음**: 모든 도구가 파일 경로를 받고 즉시 저장합니다. 열기/저장/문서 ID 관리가 없습니다.
- **통일된 주소**: `hwp_read_document`가 출력하는 주소를 그대로 씁니다.
  `p12`(본문 문단), `p3-p8`, `t0.r1.c2`(표 셀), `t0.r0.c*`(0행 전체), `t0.r1.c2.p0`(셀 안 문단).
- **고정 단위**: 글자 크기 pt, 길이 mm, 줄간격 %, 색 `#RRGGBB`.
- **부분 서식은 텍스트로 지정**: `match="매출"`처럼 지정하므로 글자 위치를 셀 필요가 없습니다.
- **명확한 오류**: 잘못된 주소/스타일/범위는 이유와 올바른 사용법을 담은 오류를 돌려주고 파일은 바뀌지 않습니다.

## 직접 실행 방식 (스킬 `hwp-direct`)

MCP 도구에 없는 기능이나 한 번에 많은 편집은 AI가 Python 스크립트로 처리합니다.

```python
from hwp_mcp.api import HwpDoc
doc = HwpDoc.open(r"C:\docs\report.hwpx")
doc.insert_paragraph("1. 서론\t3\n2. 본론\t7", after="p1")
doc.set_tabs("p2-p3", [{"pos_mm": doc.text_width_mm(), "type": "right", "leader": "dot"}])  # 목차 점선
doc.add_footnote("p5", "출처: 통계청", after_match="18.4%")
doc.save()   # 검증 후 저장, 실패하면 파일은 그대로
```

| 추가 기능 | API |
|---|---|
| 탭 설정(목차 점선), 고정/최소/여백만 줄간격 | `set_tabs`, `set_line_spacing` |
| 문단 테두리·배경 | `set_paragraph_border` |
| 각주·미주, 하이퍼링크, 책갈피 | `add_footnote`, `add_hyperlink`, `add_bookmark` |
| 수식(LaTeX), 글상자, 그림/글상자 자유 배치 | `add_equation`, `add_textbox`, `float_image` |
| 셀 대각선·방향별 안 여백·세로쓰기 | `set_cell_options` |
| 구역 추가(구역별 용지), 다단 | `add_section`, `set_columns` |
| 메모, 변경 추적, 누름틀 | `add_memo`, `track_insert/delete/replace`, `add_form_field`/`fill_form_field` |
| 글자 그림자·외곽선·양각·음각·밑줄 모양 | `format_text(shadow_color=, outline=, emboss=, ...)` |
| 그 밖의 모든 속성 | `derive` / `modify_para_pr` / `modify_char_pr` / `modify_cells` (공유 정의를 복제 후 수정) |

한컴 파일의 저장 규칙(여백·고정 줄간격·탭 위치는 `hp:default`에 두 배 값으로 저장 등)은 실제 한컴 문서와 대조해 구현했고, `skill/hwp-direct/reference.md`에 정리했습니다.

## 제한 사항

- 미리보기는 **근사치**입니다(표 테두리, 그림, 자동 번호가 생략될 수 있음). 정확한 모양은 "한컴오피스에서 열기"로 확인하세요.
- `.hwp`는 내부적으로 HWPX 모델로 변환해 편집 후 HWP 5.0으로 다시 씁니다. 변환기가 옮기지 못하는 요소가 있는 파일은 제자리 편집을 거부하고 `hwp_save_as`로 사본(.hwpx 권장)을 만들도록 안내합니다. 암호/배포용 문서는 열 수 없습니다.
- 중첩 표(셀 안의 표)는 행/열 구조 편집을 지원하지 않습니다.
- 이미지가 많은 대용량 문서(수 MB)는 편집 1회당 수 초가 걸립니다(매번 전체 검증 후 저장).
- `hwp_undo` 기록은 MCP 서버가 실행되는 동안만 유지됩니다.
- 차트는 기본 지원하지 않습니다. 이미지로 그려 삽입하세요.

## 개발

```
<venv python> -m pip install -e server
<venv python> tests/e2e_test.py              # MCP stdio로 전체 도구 시나리오 실행
<venv python> tests/api_test.py              # 직접 실행 API 전 기능 + 결과 XML 검사
<venv python> tests/skill_recipes_test.py    # 스킬 문서의 코드 블록을 그대로 실행
<venv python> tests/corpus_test.py <샘플 폴더> <작업 폴더>   # 실제 한컴 문서 대상 편집 배터리
```
