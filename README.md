# hwp_tools — AI용 한글 문서 편집 VS Code 확장

Claude Code, VS Code Copilot(에이전트 모드), Roo Code / Zoo Code, Kilo Code, Continue 같은 AI 에이전트가 **한글 문서(.hwpx / .hwp)를 직접 읽고, 찾고, 고치고, 고친 결과를 스스로 확인할 수 있게** 해 주는 VS Code 확장입니다. 로컬 LLM처럼 작은 모델을 위한 축소 도구 구성도 제공합니다.
한컴오피스 없이 동작합니다(순수 Python, [python-hwpx](https://github.com/airmang/python-hwpx) 기반).

## 구성

| 구성 요소 | 역할 |
|---|---|
| MCP 서버 (`server/hwp_mcp`) | 31개 `hwp_*` 도구. 모든 편집은 스키마·한컴 열기 안전성 검증 후에만 저장, 실패 시 파일 변경 없음 |
| 직접 실행 API (`server/hwp_mcp/api.py`) | AI가 Python 스크립트로 문서를 편집. MCP 도구 전부 + 고급 기능 + 안전한 XML 직접 편집. 마지막에 한 번 검증·저장 |
| 검증 명령 (`python -m hwp_mcp.check`) | 파일 검증, 문단 주소·서식 목록, 근사 미리보기 PNG |
| 스킬 (`skill/hwp-direct`) | Claude Code(및 full 구성의 Roo/Zoo Code) 스킬: 언제 MCP/스크립트를 쓰는지, 반드시 지킬 규칙, 검증된 레시피, OWPML XML 참조 |
| 사용 규칙 (`rules/`) | Roo/Zoo·Kilo·Continue에 설치하는 짧은 사용 규칙(도구 구성별 `hwp-basic.md`, `hwp-full.md`) |
| VS Code 확장 (`extension.js`, `clients.js`) | `~/.hwp-mcp/venv` Python 환경 자동 구성, 선택한 AI 에이전트에 MCP 서버·규칙·스킬 등록, `.hwpx/.hwp` 근사 미리보기 |

## 설치

1. `python scripts/build_vsix.py` → `dist/hwp-tools-<버전>.vsix`
2. `code --install-extension dist/hwp-tools-<버전>.vsix` (확장 ID `local.hwp-tools`; VS Code 확장 ID에는 `_`를 쓸 수 없어 `-` 사용)
3. VS Code 재시작 → 첫 실행 시 Python 환경을 자동 설치(인터넷 필요, 1회)
4. 명령 팔레트 → **hwp_tools: AI 에이전트에 HWP 도구 등록** → 쓸 에이전트를 고름(여러 개 가능) → 각 에이전트에서 MCP 서버 목록을 새로 고치거나 VS Code 창을 다시 로드
   (Claude Code만 쓸 때는 **hwp_tools: Claude Code에 HWP MCP 서버·스킬 등록** 명령도 그대로 쓸 수 있습니다.)

요구 사항: Python 3.10+, VS Code 1.101+.

### 에이전트별 등록 내용

기본값은 Claude Code만 등록하는 것이고, 나머지는 선택입니다(설정 `hwpMcp.clients`). 등록 명령은 선택한 에이전트의 설정 파일을 워크스페이스에 만들거나 기존 파일에 `hwp` 항목만 합쳐 넣습니다. 선택을 해제해도 이미 만든 파일은 지우지 않습니다.

| 에이전트 | 만드는 파일 | 도구 구성 |
|---|---|---|
| Claude Code | `.mcp.json`, `.claude/skills/hwp-direct/` | full + 스크립트 스킬 |
| GitHub Copilot | 없음 (VS Code MCP API로 등록, `hwpMcp.clients`에 있을 때만) | full |
| Roo Code / Zoo Code | `.roo/mcp.json`, `.roo/rules/hwp-tools.md` (+ full이면 `.roo/skills/hwp-direct/`) | `hwpMcp.localAgentProfile` |
| Kilo Code (7.x) | `.kilo/kilo.jsonc`의 `mcp.hwp`·`instructions`, `.kilo/rules/hwp-tools.md` | `hwpMcp.localAgentProfile` |
| Continue | `.continue/mcpServers/hwp.yaml`, `.continue/rules/hwp-tools.md` (항상 적용) | `hwpMcp.localAgentProfile` |

- 기존 `.roo/mcp.json`의 `hwp` 항목에 둔 `alwaysAllow`·`disabledTools`·`timeout` 같은 사용자 설정은 유지합니다.
- `kilo.jsonc`에 주석이 있으면 주석이 사라지지 않도록 파일을 고치지 않고, 넣을 항목을 클립보드에 복사한 뒤 파일을 열어 줍니다.
- Continue는 agent 모드에서만 MCP 도구를 씁니다.
- 예전 Kilo Code(4.x~5.x, `.kilocode/mcp.json`)는 지원하지 않습니다.

### 도구 구성 (full / basic)

Roo/Zoo·Kilo·Continue는 로컬 소형 모델로 쓰는 경우가 많아 기본 구성이 `basic`입니다. 대형 모델로 쓴다면 설정 `hwpMcp.localAgentProfile`을 `full`로 바꾸세요. 바꾸면 설정 파일을 다시 만들지 묻습니다. 서버 단독 실행은 `python -m hwp_mcp --profile basic`(또는 환경 변수 `HWP_MCP_PROFILE=basic`)입니다.

| | full | basic |
|---|---|---|
| 도구 | 31개 | 11개: `hwp_outline`, `hwp_read_document`, `hwp_search`, `hwp_get_paragraph`, `hwp_replace_text`, `hwp_set_paragraph_text`, `hwp_insert_paragraph`, `hwp_delete_paragraphs`, `hwp_set_cell_text`, `hwp_diff`, `hwp_undo` |
| 도구 정의 크기 | 약 9.7k 토큰 | 약 2.6k 토큰 |
| 서버 안내문 | 약 2.1k 자 | 약 0.8k 자 |
| 결과 크기 상한 | 읽기 2만 자, diff 8천 자, 개요 150줄 | 읽기 8천 자, diff 4천 자, 개요 60줄 |

## 도구 (31개)

| 분류 | 도구 |
|---|---|
| 문서 | `hwp_create_document`, `hwp_read_document`(전체 또는 범위: 절 `s2.1`, 문단 `p10-p40`, 표 `t3`), `hwp_get_paragraph`, `hwp_find_text`, `hwp_save_as`, `hwp_undo` |
| 탐색·검증 | `hwp_outline`(절 지도), `hwp_search`(관련 문단·표 행 순위 검색), `hwp_diff`(마지막 편집/세션 전체/다른 파일 대비 변경 내역) |
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
- **편집 결과를 바로 보여 줌**: 편집 도구는 바뀐 문단의 현재 텍스트·서식과, 문단 삽입/삭제로 뒤쪽 주소가 얼마나 밀렸는지를 함께 돌려줍니다.

### 긴 문서 탐색과 자기 검증

100쪽 안팎의 문서는 한 번에 읽을 수 없으므로(소형 모델은 더욱) 찾아서 읽습니다.

1. `hwp_outline`: 제목으로 절 지도를 만듭니다. 제목은 개요 스타일(개요 수준) → 공문서 번호 체계(`Ⅰ.` `1.` `가.` `1)` `□` ...) → 글자 크기·굵기 순으로 판별하고, 제목이 없으면 약 4천 자 단위로 나눕니다. 어떤 방법을 썼는지 결과에 표시됩니다.
2. `hwp_read_document(range="s2.1")`: 한 절만 읽습니다. 결과는 크기 제한이 있고, 잘리면 다음에 읽을 범위를 알려 줍니다.
3. `hwp_search`: 한글은 글자 2-gram, 그 밖의 단어는 통째로 색인한 BM25 검색입니다(추가 의존성 없음). 조사·어순이 달라도 찾고, 정확히 일치하는 구절은 `*`로 표시해 앞에 둡니다.
4. `hwp_diff`: undo 기록을 기준으로 바뀐 문단을 문단 단위로 맞춰 텍스트(전→후), 추가/삭제, 스타일·문단·글자·셀 서식, 쪽 설정 변경을 보여 줍니다. 편집이 의도대로 됐고 다른 곳은 바뀌지 않았는지 한 번에 확인할 수 있습니다.

주소와 절 번호는 호출할 때마다 문서에서 새로 계산하므로 편집 뒤에도 항상 현재 값입니다.

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
- `hwp_undo` 기록과 `hwp_diff`의 비교 기준은 MCP 서버가 실행되는 동안만 유지됩니다.
- 번호·글자 크기로 추정한 제목(개요 스타일이 없는 문서)은 틀릴 수 있습니다.
- 차트는 기본 지원하지 않습니다. 이미지로 그려 삽입하세요.

## 개발

```
<venv python> -m pip install -e server
<venv python> tests/e2e_test.py              # MCP stdio로 전체 도구 시나리오 실행
<venv python> tests/api_test.py              # 직접 실행 API 전 기능 + 결과 XML 검사
<venv python> tests/skill_recipes_test.py    # 스킬 문서의 코드 블록을 그대로 실행
<venv python> tests/reader_test.py           # 개요·범위 읽기·검색·diff (생성한 100쪽 규모 문서 포함)
node tests/clients_test.js                   # 에이전트별 설정 파일 작성(병합·보존·주석 있는 JSONC)
node tests/extension_smoke_test.js           # vscode 스텁으로 확장 활성화·등록 명령·Copilot 선택 등록
# node가 없으면 VS Code의 Electron으로: ELECTRON_RUN_AS_NODE=1 "<VS Code>/Code.exe" tests/clients_test.js | cat
<venv python> tests/corpus_test.py <샘플 폴더> <작업 폴더>   # 실제 한컴 문서 대상 편집 배터리
```
