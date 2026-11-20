# 상담 API 계약

## 조건 해석과 계산의 경계

- 모델은 사용자의 말을 조건으로 해석합니다. 예를 들어 “단골이 중요하다”는 말만으로 특정 지표의 가중치를 정하지 않습니다. 어떤 기준을 택하느냐에 따라 후보가 달라질 때만 질문합니다.
- 도구는 확정한 조건으로 필터링·집계·정렬합니다. 자료 버전, 후보군, 업종, 중심점, 반경, 정렬 기준이 같으면 질문 표현이나 모델 공급자가 달라도 후보 순서가 같아야 합니다. 동률은 고정된 장소 식별자로 정렬하되 우열로 설명하지 않습니다. 결측값은 0으로 대체하지 않습니다.
- 지도와 카드는 같은 계산 결과를 사용합니다. 모델이 후보를 추가하거나 재정렬하지 않습니다. 센터 제한자료를 사용하는 도구는 화면용 결과를 로컬에 두고 모델에는 완료 신호만 반환해야 합니다. 화면용 결과가 다음 턴의 모델 이력이나 트레이싱으로 전달되어서는 안 됩니다.

`POST /api/turn`에 `text`, 직전에 받은 `profile`, 선택적으로 `selection`을 보냅니다. 값이 있는 `text`는 칩 응답을 포함해 기본 OpenAI Responses (`gpt-6-luna`)로 처리합니다. `chip`은 이전 클라이언트 호환용이며 텍스트 처리를 바꾸지 않습니다. `--provider gemini`는 선택 경로입니다. OpenAI SDK와 `OPENAI_API_KEY`가 필요하며, `OPENAI_PROJECT` 및 `OPENAI_MODEL`은 선택 설정입니다. 키는 실행 환경 또는 `.env`에서만 읽습니다. 서버는 OpenAI에 대화 저장을 요청하지 않습니다(`store: false`); 브라우저는 서버가 돌려준 `profile`을 그대로 보내 제한된 최근 대화 문맥을 이어 갑니다. `--offline`은 모델과 허브 모두 끄며, 키 없음·외부 전송 제한·모델 오류도 로컬 상담으로 이어집니다. `/`는 상담 시작 주소, `/dong/<8자리 동네코드>`는 선택 동네의 직접 링크입니다. 기존 `/studio.html?dong=…`도 열리며 화면에서 새 주소로 바뀝니다.

## 응답

기존 `message`, `profile`, `slots`, `chips`, `candidates`, `candidateNames`, `excluded`, `excludedReasons`, `saturated`, `alternatives`, `advice`, `scene`과 선택적인 `card`를 유지합니다. 사진이나 실제 보행 관측값을 만들지 않습니다.

| 필드 | 모양과 의미 |
| --- | --- |
| `agentReply` | 한국어 존댓말 평서문 문자열. 모델이 없는 경로, 제한 문맥, 규칙 대체 경로에서는 빈 문자열이며 `message`가 안내를 맡습니다. 일반 모델 결과 턴에서도 모델 자신이 쓴 문장 중 숫자·후보 밖 지명·근거 없는 수치/우열/인과/지역 주장이 없는 한두 문장만 남기고, 그 첫 문장을 `notebook.summary` 뒤에 한 번 붙입니다. 후보·순위·수치·보기(`label`/`text`)는 언제나 엔진 결과입니다. |
| `agent` | `used: boolean`, `steps: Step[]`. 모델 경로의 기존 `calls: {name, args}[]`와 공급자별 메타데이터, 대체 경로의 `reason`을 유지합니다. |
| `Step` | `{tool: string, label: string, args: object, summary: string, kind: "observed" \| "assumed" \| "computed"}`. `label`은 한국어 명사구이고 `summary`는 한국어 한 줄입니다. 실제 실행 순서입니다. |
| `question` | `{text: string, options: [{label: string, text: string}]} \| null`. 모델이 상황에 맞는 질문과 보기 2~3개를 직접 만들어 `ask_user`에 보내야 하며, 값이 없으면 질문 도구가 거절합니다. 한 차례에 질문 하나만 반환하고 즉시 끝납니다. `label`은 표시용, `text`는 다음 턴에 보낼 응답입니다. 규칙 경로의 질문은 `advice.question`에 남지만 채팅 화면은 이를 에이전트 생성 선택지로 표시하지 않습니다. 최종 응답의 질문은 엔진이 고른 질문이고, 일반 모델 경로에서는 보기 집합이 같을 때만 모델의 문구를 씁니다. |
| `focus` | 동네 코드 또는 `null`. `profile.focus`와 `scene.fly`도 계속 동네 코드(또는 `null`)를 받습니다. |
| `focusLevel` | `"island" \| "dong" \| "building"`. |
| `focusTarget` | 동네는 동네 코드, 건물은 아래 건물 대상 ID, 제주 전체는 `null`. |
| `population` | 인구 도구 실행 시 기존 허브 집계 모양에 `kind: "observed"`를 추가합니다. `source`는 `live` 또는 `snapshot`, `date`는 기준일입니다. 자료가 없으면 `null`입니다. `card.population`에도 같은 표식을 씁니다. |
| `walk` | 보행 도구 실행 시 기존 `/api/walk` 결과를 그대로 전달하고 `kind: "assumed"`를 추가합니다. 실제 사람 이동 관측이 아닙니다. |
| `nearby` / `comparison` | 각각 조사한 주변 점포와 답사 이유, 두 동네 카드에서 서로 다른 지표만 담습니다. |

두 공급자는 모델 호출을 한 차례에 최대 여섯 번 실행합니다. 도구 결과를 다시 모델에 보내되, 점포 수·허브 집계·좌표 및 보행 수치는 모델에 보내지 않습니다. `population` 단계는 `observed`, `walk_route` 단계는 `assumed`, 나머지는 `computed`입니다.

## 지도와 대화 연결

`text` 없이 `{"mapFocus":{"level":"dong","target":"50130320"},"profile":…}`를 보내면 모델을 부르지 않고 즉시 초점을 반환합니다. 기존 `{"dong":"50130320","text":""}`도 동작합니다. 제주 전체로 돌아갈 때는 `{"mapFocus":{"level":"island","target":""},"profile":…}`를 보냅니다.

건물 ID는 `<동네코드>:<레이어>:<인덱스>`입니다. 표선면 스튜디오 윤곽은 `50130320:new:0`처럼 `new`(`features`) 또는 `old`의 0부터 시작하는 원본 배열 인덱스를 사용합니다. 상가 지도에서는 `50130320:store:53823`처럼 등록 점포 위치를 대리 대상으로 사용할 수 있으며 실제 입점 건물과 같은 뜻은 아닙니다. 응답의 `focusTarget`을 그대로 사용하세요. 지도를 옮긴 응답은 대화 기록이나 창업 조건을 지우지 않습니다.

`selection`에는 현재 동네와 건물의 세계 좌표 `{dong, x, z}`, 또는 등록 점포 `{type:"store", dong, store}`를 보낼 수 있습니다. `inspect_place`는 두 경우 모두 현재 선택 위치를 사용할 수 있고, `walk_route`는 후자의 점포를 기본으로 사용할 수 있습니다.

상담 화면은 섬 전체를 2D로 열고, 동네를 고르면 같은 지도 영역에서 `/index.html?embed=1&dong=…`의 3D 장면으로 전환합니다. `2D 지도`/`3D 거리`로 가까운 동네에서도 두 표현을 오갈 수 있습니다. `walk` 경로의 움직이는 보행자는 **가정 보행 예시**이며 관측한 유동인구를 나타내지 않습니다.
