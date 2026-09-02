# AI-PMO 시작하기

> 원본: 일본어판과 영어판이 원본이며, 다른 언어는 번역본입니다.

---

## 이것은 무엇인가요

프로젝트 관리(PMO) 업무를 AI에게 맡기기 위한 도구입니다.

예를 들어 이런 일을 할 수 있습니다.

- Teams 회의 기록에서 **회의록을 자동으로 만들기**
- 회의록에서 **"누가, 무엇을, 언제까지"를 뽑아 작업으로 등록하기**
- 기한이 지난 작업의 담당자에게 **자동으로 독촉 보내기**

"템플릿"이라는 설계도를 고르기만 하면 동작합니다.
프로그래밍 지식은 필요 없습니다.

---

## 누구를 위한 도구인가요

- **학생** — 프로젝트 관리의 틀을 배우면서 쓸 수 있습니다
- **중소기업** — 전담 PMO가 없어도 일하는 틀을 얻을 수 있습니다
- **대기업** — 부서마다 제각각인 방식을 템플릿으로 맞출 수 있습니다

**전부 무료입니다.** 기능 제한판도 체험판도 아닙니다.
템플릿과 프롬프트도 같은 조건입니다.
(AI 서비스 이용료는 선택한 제공자에게 직접 지불합니다.)

---

## 쓰려면 필요한 것

| | 필요한 것 | 비용 |
|---|---|---|
| **간단 구성** | 컴퓨터, AI 서비스의 API 키 | AI 이용료(종량제·소액) |
| **사내 구성** | Docker, 메모리 16GB 이상, 가능하면 GPU | 무료(전기요금만) |

> **어느 쪽을 고를까요?**
> 우선 써 보려면 **간단 구성**.
> 회의 내용을 외부 서비스로 보낼 수 없다면 **사내 구성**입니다.

---

## 세 단계로 시작하기

### 1. 설치하기

[INSTALL.md](../../INSTALL.md)의 순서를 따라 주세요.

- **Windows** — `AI-PMO-Setup.exe` 더블클릭
- **Mac / Linux** — 터미널에서 `./scripts/install.sh`
- **Docker** — `./scripts/install-docker.sh`

### 2. 설정하기

설치가 끝나면 설정 화면이 자동으로 열립니다.
질문에 답해 주세요. 모르겠으면 Enter를 누르면 기본값이 됩니다.

```
1) AI를 어디에서 실행할까요?      → 1 (클라우드)
2) AI 제공자를 선택하세요         → 1 (OpenAI)
3) API 키를 입력하세요            → 붙여넣기
4) 조직을 구분할 이름             → 회사명 등 (영소문자)
5) 데이터베이스 연동을 쓸까요?     → N
```

**제공자는 네 곳 중에서 고를 수 있습니다.** 망설여지면 OpenAI를 고르세요.
임베딩 기능까지 갖춰져 있어 설정 하나로 끝납니다.

| 제공자 | 특징 |
|---|---|
| OpenAI | 망설여지면 이것 |
| Gemini | 긴 회의 기록을 저렴하게 처리 |
| Groq | 빠름. 다만 키가 두 개 필요 |
| OpenRouter | 키 하나로 여러 모델을 시험 가능 |

**API 키 발급 방법**
고른 제공자의 사이트에서 계정을 만들고 키를 발급합니다.
긴 문자열입니다. 다른 사람에게 보여 주지 마세요.

- OpenAI — https://platform.openai.com/api-keys
- Gemini — https://aistudio.google.com

자세한 내용은 [PROVIDERS.md](../PROVIDERS.md)를 보세요.

### 3. 실행해 보기

```bash
aipmo validate templates/examples/meeting_minutes.yaml
```

이렇게 표시되면 성공입니다.

```
OK  templates/examples/meeting_minutes.yaml  [software] ステップ 5 件
```

`ステップ 5 件` 은 “5단계”라는 뜻입니다. 도구의 출력은 일본어입니다.

---

## 템플릿이란

"어떤 순서로 무엇을 하는지"를 적은 설계도입니다.
이것 하나가 하나의 PMO 업무에 대응합니다.

```yaml
name: meeting_minutes          # 이름
trigger: "event:teams:meeting_ended"   # 기동 계기 (자동으로는 돌지 않음)

steps:                         # 무엇을 할지
  - id: fetch_transcript       # ① 회의 기록을 가져온다
    adapter: teams

  - id: minutes                # ② AI에게 회의록을 쓰게 한다
    llm: { profile: default }

  - id: register_jira          # ③ 작업을 등록한다
    adapter: jira
```

`event:` 는 “무엇이 계기인지”를 적는 칸입니다. 회의가 끝나도 자동으로는 돌지 않습니다.
`aipmo run` 이나 휴대폰 화면에서 회의 정보를 넘겨 실행합니다.
정해진 시각에 돌리려면 `trigger: "schedule:..."` 와 `aipmo schedule` 입니다.

하고 싶은 일이 바뀌면 템플릿을 바꾸기만 하면 됩니다.
**AI를 쓰는 방식 자체가 템플릿에 따라 달라집니다.**

---

## 자주 쓰는 조작

```bash
aipmo setup       # 설정을 다시 하기
aipmo validate <파일>   # 템플릿에 잘못이 없는지 확인
aipmo run <파일>        # 실행
aipmo adapters    # 연결된 외부 도구 목록
aipmo doctor      # 연결이 되는지 확인
aipmo serve       # 휴대폰용 화면 열기
aipmo schedule    # 정해진 시각의 자동 실행 시작
```

---

## 안전을 위해 알아둘 것

**API 키는 `.env`에 저장됩니다.** `config.yaml`에는 들어가지 않습니다.
설정 파일은 동료와 공유하거나 Git에 올리는 것이므로,
키가 섞이지 않도록 나누어 두었습니다.

**사내 데이터는 밖으로 나가지 않습니다.** 회사마다 데이터 보관 위치가 나뉘어 있고,
다른 회사의 데이터에는 기술적으로 닿을 수 없게 되어 있습니다.

**공개는 자동으로 이루어지지 않습니다.** 템플릿에서 공개 저장소에 쓸 수는 없습니다.
후보를 내는 것까지는 가능하지만, 사람이 승인해 공개하는 절차는 아직 없습니다.

---

## 잘 안 될 때

**`aipmo`라고 입력해도 "찾을 수 없습니다"라고 나온다**
Mac / Linux라면 다음을 실행한 뒤 터미널을 다시 열어 주세요.
```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
```

**Windows에서 `.ps1`을 더블클릭해도 아무 일도 일어나지 않는다**
`install.bat` 쪽을 더블클릭해 주세요.

**API 키를 넣는 것을 잊었다**
`aipmo setup`을 한 번 더 실행해 주세요.

**백신 프로그램이 설치 파일을 막는다**
서명이 없는 파일은 경고가 뜰 수 있습니다.
신경 쓰인다면 Mac / Linux판이나 Docker판을 써 주세요.

더 자세한 내용은 [INSTALL.md](../../INSTALL.md)를 보세요.

---

## 다음에 읽을 것

- [INSTALL.md](../../INSTALL.md) — 설치 상세
- [MOBILE.md](../MOBILE.md) — 휴대폰에서 쓰기
- [PROVIDERS.md](../PROVIDERS.md) — AI 제공자 고르는 법
- [AGENTS.md](../AGENTS.md) — AI가 스스로 판단하게 하기
- [TEAMS.md](../TEAMS.md) — Teams 회의 기록과 연결하기
- [JIRA-SLACK.md](../JIRA-SLACK.md) — Jira 등록과 Slack 알림
- [SCHEDULER.md](../SCHEDULER.md) — 정해진 시각에 자동으로 실행하기
- [AGILE.md](../AGILE.md) — 스프린트 상황 보고하기
- [INDUSTRIES.md](../INDUSTRIES.md) — 건설·마케팅 등 업종별 활용
- [LICENSE](../../LICENSE) — MIT 라이선스 (상업적 이용·수정·재배포 가능)
- [README.md](../../README.md) — 구조와 설계 (개발자용)
- `templates/examples/` — 템플릿 실제 예
