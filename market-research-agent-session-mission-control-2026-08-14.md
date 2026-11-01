# Market Research: Agent Session Mission Control

**Date:** 2026-08-14
**Decision this supports:** positioning (build vs wrap first-party vs don't-build-as-SaaS)
**Confidence:** medium — competitor map and user pain are well sourced; TAM for this *specific* niche is inferred, not independently sized

**Inferred inputs (not confirmed with founder):**
- **Product:** 여러 코딩 에이전트 세션(Grok Build / Claude Code / Codex 등)이 동시에 돌 때, 세션별 **목표(goal)** 와 **진행상황(progress)** 을 한 화면에서 보는 서비스
- **Target:** 개인 파워유저 개발자 우선 (B2C/dev-tool). 팀/B2B는 후순위 가정
- **Decision:** 기존 서비스 조사 후 무엇을 만들지 정하기

> Market data is skewed toward US/global English sources. Korean local signal is underweighted. See watch-out checklist.

## TL;DR
- 사용자가 원하는 것은 LangSmith류 **프로덕션 에이전트 관측**이 아니라, Grok Build처럼 **로컬에서 동시에 도는 코딩 에이전트 세션의 목표·진행 관제**다. 이 두 시장은 겹쳐 보이지만 구매자·가격·제품이 다르다.
- 세션 목록 + 상태(working / needs input / done)는 이미 1st party가 빠르게 먹고 있다. Grok Build Dashboard, Claude Code Agent View, Codex app, Cursor Agents Window, VS Code Agent Sessions가 2026년에 거의 동시에 출시됐다.
- 그래도 사람들이 직접 대시보드를 만들고 있다. 이유는 **목표/진행이 구조화되어 보이지 않고**, **벤더가 갈리면 한 화면에 안 모이며**, **승인이 필요해도 탭을 찾아다녀야** 하기 때문이다.
- 가장 가까운 상용 제품은 Conductor(로컬 무료 / Pro $50 / Teams $60/user). 오픈소스는 Vibe Kanban(27.8k★), Claude Squad(8.3k★), CCManager(1.2k★), Clorch, Gas Town.
- **권고: 범용 AI agent SaaS를 만들지 말 것.** Grok의 `/goal` + `plan.json` + Dashboard를 먼저 연결하는 **로컬 관제면**으로 시작하고, 차별점은 “상태 한 줄”이 아니라 **세션별 목표·남은 할 일·증거 갭**이다.

## 1. Competitive Landscape

시장은 두 층이다. 혼동하면 잘못된 경쟁자를 고른다.

| Layer | Job to be done | Typical buyer | Examples |
|-------|----------------|---------------|----------|
| A. Production agent observability | 우리가 *만든* 에이전트 앱의 trace/eval/cost | 플랫폼/ML 팀 | LangSmith, Langfuse, Phoenix, Braintrust |
| B. Coding-agent session mission control | 내가 *돌리는* 코딩 세션들의 목표·진행·승인 | 개인 개발자 / 소규모 팀 | Grok Dashboard, Claude Agent View, Conductor, Claude Squad |

사용자가 말한 문제는 **Layer B**다.

### Direct competitors

| Name | What it does | Pricing | Scale signal | Last active | Source |
|------|--------------|---------|--------------|-------------|--------|
| **Grok Build Dashboard** | 같은 pager의 top-level 세션을 상태별로 나열. peek/reply/dispatch. activity line (`Running: cargo test`) | Grok에 포함 | 1st party | 2026-08 (docs) | Grok dashboard docs |
| **Grok `/goal` + `plan.json`** | 세션 단위 자율 목표 + TODO 파일. `/goal status/pause/resume/clear`. Dashboard 행에는 아직 목표가 1급으로 안 보임 | 포함 | 세션 저장소에 `plan.json` 존재 | 2026-08 | slash commands, sessions |
| **Claude Code Agent View** (`claude agents`) | 백그라운드 세션 테이블. Needs input / Working / Completed. Haiku 한 줄 요약, peek/reply, PR 상태, worktree 격리 | Claude Pro $20 / Max $100–$200 | Claude Code ~134k GitHub ★ | 2026, research preview | [agent-view docs](https://code.claude.com/docs/en/agent-view) |
| **Claude Code `/goal`** | 완료 조건을 걸면 turn마다 Haiku evaluator가 충족 여부를 판정. status에 조건/시간/turn/토큰 | 포함 | 9시간 자율 런 사례 | 2026 | [goal docs](https://code.claude.com/docs/en/goal) |
| **Conductor (Melty Labs, YC S24)** | Mac에서 Claude/Codex/Cursor를 worktree로 병렬 실행. 대시보드 + diff review + Linear | Free 로컬 / Pro $50 / Teams $60/user / Ent custom | Series A (2026-03-30), team size 6 | 2026-08, v0.81 | [conductor.build](https://www.conductor.build), [pricing](https://www.conductor.build/pricing), [YC](https://www.ycombinator.com/companies/conductor) |
| **Vibe Kanban** | 칸반 카드 = worktree = 에이전트. Claude/Codex/Gemini/Amp 등 | OSS, BYOK. Bloop는 2026-04 셧다운 후 커뮤니티 유지 | **27.8k ★**, 3.0k forks | 2026 cloud update 논의 | [GitHub](https://github.com/BloopAI/vibe-kanban), [Nimbalyst 비교](https://nimbalyst.com/blog/best-multi-agent-coding-tools-2026/) |
| **Claude Squad (`cs`)** | tmux + git worktree로 여러 CLI 에이전트 관리 | OSS, AGPL-3.0 | **8.3k ★**, 599 forks | 2026 (220 commits) | [GitHub](https://github.com/smtg-ai/claude-squad) |
| **CCManager** | tmux 없이 다중 CLI(Claude/Gemini/Codex/Cursor/Copilot/Cline/OpenCode/Kimi) 세션. waiting/busy/idle | OSS (`npm i -g ccmanager`) | **1.2k ★**, 91 forks, 747 commits | 2026 | [GitHub](https://github.com/kbwo/ccmanager) |
| **Clorch** | Claude hooks → `/tmp` JSON → TUI. 승인/거부, YOLO, jump | OSS, MIT, pip | 소규모 (95 commits) | 2026 | [GitHub](https://github.com/androsovm/clorch) |
| **Claude Pulse** | Mac notch에 세션 점(gray/green/amber). Cmd+Y로 승인 | Free macOS | 제품 사이트 + 블로그 | 2026-04~05 | [claudepulse.app](https://claudepulse.app/blog/managing-multiple-claude-sessions) |
| **Gas Town (Steve Yegge)** | 20–30 에이전트용 “Kubernetes for coding agents”. Mayor/Convoy/Beads/dashboard | OSS, MIT. `gt dashboard` 웹 | 7,770 commits, Homebrew `gastown` | 2026 | [GitHub](https://github.com/gastownhall/gastown) |
| **Nimbalyst** | 데스크톱 칸반 + worktree + 멀티에디터. Claude/Codex 1급 | Free core + paid advanced | 신생, 비교 글의 저자 제품 | 2026-04 | [nimbalyst.com](https://nimbalyst.com/blog/best-multi-agent-coding-tools-2026/) |
| **Cursor 3 Agents Window / Glass** | IDE 안 타일/클라우드 에이전트. 관제가 에디터보다 앞면이 되는 중 | Cursor 구독 | 1st party | 2026-04 | [Addy Osmani](https://addyosmani.com/blog/code-agent-orchestra/), [Nimbalyst](https://nimbalyst.com/blog/best-multi-agent-coding-tools-2026/) |
| **Codex app** | OpenAI의 멀티 에이전트 커맨드 센터. worktree, 병렬 세션 | ChatGPT/Codex 구독 | 2026-02 macOS, 03 Windows | 2026-04 v2 | [Nimbalyst timeline](https://nimbalyst.com/blog/best-multi-agent-coding-tools-2026/) |
| **VS Code Agent Sessions** | 로컬/백그라운드/클라우드 에이전트를 한 뷰 | VS Code 포함 | 1.109 (2026-01) | 2026-02 | [VS Code blog](https://code.visualstudio.com/blogs/2026/02/05/multi-agent-development) |

### Adjacent tools & status-quo alternative

**Layer A — 프로덕션 관측 (잘못된 비교 대상이지만 투자/가격 벤치마크):**
- [LangSmith](https://www.langchain.com/pricing): Developer $0 (5k traces) / Plus $39/seat + usage / Enterprise custom
- [Langfuse](https://langfuse.com/pricing): Hobby free 50k units / Core $29 / Pro $199 / Ent $2,499. 2026-01 ClickHouse 인수 ([announcement](https://clickhouse.com/blog/clickhouse-acquires-langfuse-open-source-llm-observability))
- Arize Phoenix, Braintrust, AgentOps, Confident AI, Datadog LLM Obs

**1st-party 클라우드 비동기 에이전트 (세션을 “맡기고 떠나는” 쪽):**
- Claude Code on the web, GitHub Copilot Coding Agent, Jules, Codex Web, Cursor Cloud Agents

**상태 quo (do-nothing):**
- 터미널 탭 / tmux pane을 눈으로 훑기
- Linear/Jira 이슈 = 목표, 에이전트는 워커
- 개인 HTML “goal ledger”, `tasks.json`, `.omc/ultragoal` 같은 파일
- 세션 파일을 직접 파싱하는 개인 대시보드 ([ksred, 2025-07](https://www.ksred.com/managing-multiple-claude-code-sessions-building-a-real-time-dashboard/))

### White space

1st party가 **이미 잘하는 것**
- 세션 목록, 상태(working / needs you / done), peek, dispatch
- 같은 벤더 안에서의 백그라운드 실행 + worktree 격리
- Claude는 Haiku 한 줄 요약 + PR 배지. Grok은 activity line + token/tool count

**아직 약한 것 (사용자가 말한 바로 그 구멍)**
1. **목표를 1급 필드로 보여주지 않음.** Grok은 `/goal`과 `plan.json`이 있지만 Dashboard 행은 `Running: cargo test` 수준의 활동만 표시. Claude Agent View도 요약 문장이지 evaluator 조건·남은 할 일·증거 갭이 아니다.
2. **벤더 횡단.** Grok + Claude + Codex를 한 화면에 모으는 1st party는 없다. CCManager/Vibe Kanban/Conductor가 여기로 가고 있으나 목표 모델이 제각각이다.
3. **진행의 구조화.** “지금 뭘 하는 중”은 많다. “목표 대비 몇 % / 어떤 증거가 남았는지 / 왜 막혔는지”는 드물다.
4. **주의 라우팅.** Claude Pulse가 notch로 가장 가까이 갔고, Clorch가 승인 큐를 풀었다. 대부분은 여전히 탭 사냥.
5. **프로세스/머신 밖 가시성.** Grok Dashboard는 *이 pager 프로세스*의 top-level 세션만. 다른 터미널·다른 머신·폰은 별개.

## 2. Market Size
- **TAM (adjacent, not this product):** LLM observability platform **$1.97B (2025) → $2.69B (2026)**, 2030 **$9.26B**, CAGR 36.2%. [The Business Research Company, 2026-07](https://www.thebusinessresearchcompany.com/report/large-language-model-llm-observability-platform-global-market-report). 방법론: 소프트웨어+서비스, 클라우드/온프렘, 엔터프라이즈 모니터링. **코딩 세션 관제가 아님.** Single-source analyst estimate.
- **Demand proxy (agent engineering):** LangChain이 1,300+명을 조사해 **57%가 에이전트를 프로덕션에**, **89%가 observability를** 도입. 일상에서 가장 많이 쓰는 에이전트는 **코딩 에이전트**(Claude Code, Cursor, Copilot, Windsurf). [State of Agent Engineering, 2026-06-12](https://www.langchain.com/state-of-agent-engineering)
- **SAM (inferred):** 동시에 3개 이상 코딩 세션을 돌리는 파워유저. Claude Max($100–200/mo) + Codex/Cursor/Grok 구독자 중 상위 사용층. 공개된 정확한 헤드카운트 없음.
- **SOM (12–24개월, inferred):** 로컬 OSS/플러그인으로 수백~수천 명. 유료 SaaS로 가려면 Conductor Pro($50)와 1st party 번들 사이에 끼인다. 현실적 초기 매출은 “거의 0에 가깝고, 본인 워크플로 해결이 1차 ROI”.
- **Confidence:** TAM 숫자는 인접 시장·단일 출처라 **낮음**. SAM/SOM은 가정. 수요의 *존재*는 GitHub 이슈·개인 대시보드 난립으로 **높음**.

## 3. User Needs & Pain Points

### Top pain points (ranked by frequency of mention)
1. **탭을 돌며 상태를 확인하는 비용** — Reddit, 개인 블로그, GitHub feature request, Clorch/Pulse 랜딩 카피가 모두 같은 말을 함
2. **어느 세션이 승인을 기다리는지 모름** — Clorch, Pulse, CCManager가 이 한 가지를 위해 존재
3. **세션이 무엇을 하고 있는지 한 줄로 안 보임** — Claude Agent View가 Haiku 요약으로 대응. 사용자는 그 위에 **목표 대비 진척**을 원함
4. **병렬 세션이 같은 파일을 망가뜨림** — worktree가 사실상 표준 해법 (Conductor, Squad, Claude bg isolation, Gas Town hooks)
5. **조율 오버헤드가 병렬의 이득을 잠식** — 같은 버그를 여러 에이전트가 고침, 설계가 사라짐 ([Nimbalyst / Gas Town 조기 사용자](https://nimbalyst.com/blog/best-multi-agent-coding-tools-2026/))
6. **검증이 병목** — 생성은 싸졌고, 맞는지 확인이 비싸다 ([Addy Osmani, 2026-03](https://addyosmani.com/blog/code-agent-orchestra/))

### Direct quotes
- “If I have more than three Claude sessions open, I'll leave the dashboard open in a browser tab. It shows me which sessions are actively processing, which directories they're running in, how many files have been changed, and the token usage over time.” — Kyle Redelinghuys, [ksred.com, 2025-07-24](https://www.ksred.com/managing-multiple-claude-code-sessions-building-a-real-time-dashboard/)
- “If I have more than three Claude sessions open… The challenge became clear when I realised I was spending time hunting for sessions than actually working in them.” — 같은 글
- “If you use Claude Code for multiple projects, you know the drill: a dozen terminal tabs, no idea which session is waiting for permission, which one errored out 20 minutes ago, and which one is actually doing work.” — Michil Androsov on Clorch, [LinkedIn](https://www.linkedin.com/posts/michil_i-run-10-claude-code-sessions-at-once-activity-7432874482387734530-Tfna)
- “Developer has 5+ Claude Code instances running across different repos… needs to see which instances are active, what they're working on, and their progress — without switching windows.” — [anthropics/claude-code#26394](https://github.com/anthropics/claude-code/issues/26394), 2026-02-17 (closed as not planned; Agent View가 이후 등장)
- “If you're checking on sessions roughly 20 times per hour… you're burning 6–10 minutes every hour just on status checks. Over a full workday, that's 50–80 minutes gone.” — [Claude Pulse blog, 2026-04-14](https://claudepulse.app/blog/managing-multiple-claude-sessions) (자사 마케팅, 수치는 검증되지 않음)
- “Claude Squad doesn't show session states in its menu, making it hard to know which sessions need attention.” — [CCManager README](https://github.com/kbwo/ccmanager)
- “You used to pair with one AI. Now you manage an agent team.” — [Addy Osmani, 2026-03](https://addyosmani.com/blog/code-agent-orchestra/)
- “For most people, two to four is the practical sweet spot. More than that only helps if you also have a good review and coordination layer.” — [Nimbalyst, 2026-04](https://nimbalyst.com/blog/best-multi-agent-coding-tools-2026/)

## 4. Pricing

### Competitor pricing

| Competitor | Free tier | Entry paid | Team | Enterprise |
|------------|-----------|------------|------|------------|
| Grok Dashboard / `/goal` | 제품에 포함 | Grok 구독 | — | — |
| Claude Agent View / `/goal` | Pro에 포함 | Pro $20 / Max $100–$200 | Team Premium seat ~$100–$150 | custom |
| Conductor | $0 로컬, BYOK | Pro $50/mo (cloud hours, multiplayer ≤5, API) | $60/user/mo (5+, admin, SSO) | custom (DPA, SCIM, SLA) |
| Claude Squad / CCManager / Clorch / Vibe Kanban | $0 OSS | — | self-host | — |
| Claude Pulse | $0 macOS | — | — | — |
| Nimbalyst | Free core | subscription for advanced | — | — |
| Langfuse Cloud (adjacent) | Hobby 50k units | Core $29 | Pro $199 + Teams add-on $300 | $2,499 |
| LangSmith (adjacent) | $0, 1 seat, 5k traces | Plus $39/seat + usage | 무제한 seat | custom, self-host |

### Pricing takeaways
- **Layer B(코딩 세션 관제)는 거의 공짜가 기본값이다.** 1st party에 묶여 있고, OSS 래퍼가 많고, Conductor도 로컬은 $0. 사람들이 이미 $20–200을 *모델*에 내고 있어서 관제에 또 $50를 내는 이유는 “클라우드에서 노트북을 닫아도 돌아가게” 또는 “팀이 같이 본다”일 때만 성립한다.
- **Layer A는 seat + usage.** 이 가격표를 코딩 세션 관제에 가져오면 과하다. 개인 개발자는 거부한다.
- 유료화 가설이 있다면 Conductor Pro와 같은 **클라우드 지속 실행 + 모바일 + 팀 공유**이지, “세션 목록 SaaS”가 아니다.

## 5. Trends
- **멀티 에이전트는 2026년 테이블 스테이크.** 2–4월에 Codex app, Claude Cowork, Cursor 3 Agents Window, Windsurf Wave 13, Codex multi-agent v2가 연달아 나왔다. [Nimbalyst timeline](https://nimbalyst.com/blog/best-multi-agent-coding-tools-2026/)
- **관제면이 에디터를 밀어내는 중.** Cursor Glass, Claude Agent View, Grok Dashboard, VS Code Agent Sessions. Osmani: “the control plane is becoming the primary experience, and the editor is one instrument underneath it.” [orchestra](https://addyosmani.com/blog/code-agent-orchestra/)
- **`/goal`이 2026 에이전트 프리미티브.** Claude와 Grok이 같은 패턴(조건 + 독립 evaluator + 자동 재시작)을 내장. 사용자는 이미 “목표를 세션에 거는” 습관이 생기고 있다. [Claude /goal](https://code.claude.com/docs/en/goal), Grok /goal
- **조율이 다음 병목.** 병렬 자체보다 공유 태스크 리스트, 파일 락, typed schema, Beads. Agent Teams / Gas Town / GitHub Agentic Workflows. [Osmani](https://addyosmani.com/blog/code-agent-orchestra/), [GitHub](https://github.com/features)
- **관측 시장은 인수·대형화.** Langfuse→ClickHouse (2026-01, ClickHouse $400M Series D / $15B 밸류). [Reuters](https://www.reuters.com/technology/database-management-firm-clickhouse-valued-15-billion-amid-ai-boom-2026-01-16/), [ClickHouse blog](https://clickhouse.com/blog/clickhouse-acquires-langfuse-open-source-llm-observability)
- **Vibe Kanban 회사(Bloop) 셧다운** — 칸반 오케스트레이션은 수요가 있어도 독립 SaaS로 버티기 어렵다는 신호. 프로젝트는 커뮤니티 OSS로 이전. [Nimbalyst](https://nimbalyst.com/blog/best-multi-agent-coding-tools-2026/)
- **Gartner (단일 인용, 원문 페이월/봇 차단):** 2028년까지 LLM observability 투자가 GenAI 배포의 50%를 차지할 것이라는 예측이 2차 자료에 반복됨. [MarkTechPost, 2026-08-09](https://www.marktechpost.com/2026/08/09/top-llm-observability-and-evaluation-platforms-in-2026-langfuse-langsmith-braintrust-arize-and-more-compared/). 원문 미확인.

## 6. Recommendation
**Verdict (updated 2026-08-14 after founder constraint):** Build a **vendor-neutral session control plane**, not a Grok wrapper and not a LangSmith clone. “모든 에이전트”는 마법이 아니라 **계약(스키마) + 어댑터**다.

### Founder constraint
범용 서비스. Grok 전용이 아님.

이 제약은 어제 권고의 “Grok Dashboard에 컬럼을 붙이자”를 기각한다. 대신 제품의 핵심 자산은 UI가 아니라 **세션·목표·진행의 정규화 모델**이다. Grok은 첫 어댑터일 뿐 제품 자체가 아니다.

### What “universal” can mean
“모든 AI agent”를 글자 그대로 한 번에 먹으면 LangSmith/A2A 영역으로 흩어진다. 실행 가능한 범위는 두 단계다.

1. **v1 표면:** 코딩 에이전트 CLI/IDE (Grok, Claude Code, Codex, Gemini CLI, Cursor Agent, Copilot CLI, OpenCode, Cline). 사용자가 처음에 말한 고통이 여기 있다.
2. **계약은 에이전트 종류에 묶지 않음:** Session / Goal / PlanEntry / Attention / Evidence. LangGraph·고객지원 봇은 같은 ingest API로 나중에 붙인다.

“어댑터 없이 아무 에이전트나 자동 관측”은 불가능하다. 벤더가 세션 파일을 숨기거나 `/goal`이 없으면 볼 것이 없다. 범용 제품의 정의는 **에이전트가 우리 계약을 말하거나, 우리가 그 에이전트 방언을 번역하는 것**이다.

### How agents actually connect (2026)

이미 있는 표준을 새로 만들지 말고 얹는다.

| Ingress | What you get | Who already speaks it |
|---------|--------------|------------------------|
| **ACP `session/update` (plan)** | 실시간 plan entries: pending / in_progress / completed | Zed Agent Client Protocol. Grok은 `updates.jsonl`이 ACP 스트림. Claude/Codex/Gemini/Copilot/Cline/OpenCode에 ACP 어댑터 존재. [ACP plan](https://agentclientprotocol.com/protocol/v1/agent-plan), [agents list](https://agentclientprotocol.com/get-started/agents) |
| **Filesystem adapter** | 디스크에 남은 세션/플랜 | Grok `plan.json`+`signals.json`, Claude `~/.claude/projects`, Codex rollouts |
| **Hooks adapter** | 승인/상태 이벤트 | Claude hooks (Clorch가 증명한 길), Gemini/Codex 훅이 있는 곳 |
| **MCP ingest tool** | 에이전트가 직접 `report_goal` / `update_plan` 호출 | 훅이 없는 모든 에이전트. AGENTS.md + MCP 서버 한 개로 “범용”이 됨 |
| **OTEL GenAI** (나중) | 프로덕션 에이전트 앱 | 아직 Development. 코딩 세션 목표가 아님 |

제품은 Conductor처럼 에이전트를 *실행*하지 않는다. 실행은 각자 CLI가 하고, 우리는 **버스 + 보드**다. 이게 Vibe Kanban/CCManager(런처) 및 LangSmith(트레이스)와 겹치지 않는 자리입니다.

### 90-day shape
1. 정규화 스키마 + 로컬 daemon + 웹/TUI 보드
2. 어댑터 3개: Grok 파일, Claude hooks 또는 ACP, MCP ingest (이게 “모든 에이전트” 입구)
3. 한 행: vendor · goal · 3/8 plan · needs-you · last evidence
4. 오케스트레이션(스폰, merge queue)은 하지 않음

**Suggested positioning:** “어떤 코딩 에이전트든, 세션의 목표와 남은 증거를 한 보드에 올리는 컨트롤 플레인.”

**Suggested pricing hypothesis:**
- 로컬 daemon + MCP/ACP ingest: $0 (채택이 전부)
- 클라우드 싱크 / 폰 / 팀 보드: $12–20/user. Conductor Pro($50)보다 싸고, 런처가 아니라 관제만 판다.

**Reasoning:**
사용자가 느낀 고통은 진짜다. 2025–2026에 Clorch, Pulse, CCManager, 개인 웹 대시보드, GitHub #26394가 같은 문장으로 반복된다. 동시에 그 “세션 목록 + 상태” 레이어는 Claude·Grok·Codex·Cursor·VS Code가 제품 안에 넣고 있다. 여기서 또 하나의 래퍼 TUI/SaaS를 만들면 6개월 안에 1st party에 먹힌다. Bloop/Vibe Kanban이 별 2.7만을 받고도 회사가 접힌 점이 그 리스크를 보여 준다.

빈틈은 **목록이 아니라 목표의 가시성**이다. Grok은 이미 재료를 갖고 있다. `~/.grok/sessions/<cwd>/<id>/plan.json`(TODO), `signals.json`(토큰/툴), `/goal`(조건 + 독립 검증 + pause/resume), Dashboard(상태 + activity). 없는 것은 이 네 가지를 **한 행에 모으는 일**이다. 예: `goal: auth 마이그레이션 · 5/8 todos · blocked: 증거 부족 · 12m · 1.2k tok`. Claude Agent View의 Haiku 요약보다 감사 가능하고, LangSmith trace보다 개발자 세션에 가깝다.

그다음에야 Claude `~/.claude` / Codex `~/.codex` 어댑터를 붙여 **벤더 횡단**을 한다. 이게 CCManager/Conductor와 겹치기 시작하는 지점이고, 차별점은 계속 “structured goal + evidence”여야 한다. 오케스트레이션(에이전트 스폰, worktree, merge queue)은 Gas Town/Conductor 영역이니 초기에 손대지 않는 편이 낫다.

**Suggested positioning:** “여러 코딩 세션의 *목표와 남은 증거*를 한눈에 보는 로컬 관제. 에이전트를 대신 돌리지 않는다.”

**Suggested pricing hypothesis:**
- v0: 본인용 로컬 (가격 $0). Grok `plan.json`/`/goal`을 Dashboard 또는 작은 웹/TUI에 투영
- v1: Claude/Codex 어댑터도 $0 OSS
- 유료는 **노트북을 닫아도 보이는 원격 뷰 / 폰 / 팀 공유**가 생길 때만. 개인 $8–15/mo 또는 Conductor보다 싼 $20 이하. $39/seat LangSmith 모델은 금지.

## Watch-Out Checklist

### Unknowns
- [ ] **타깃이 본인 도구인지, 판매할 제품인지** — 이 조사는 후자를 가정하면 Don't-build-SaaS, 전자면 Build-local이 된다. 다음 대화에서 확정할 것.
- [ ] **Grok Dashboard가 이미 `/goal`·`plan.json`을 peek에 넣는지** — 공개 유저 가이드 기준으로는 activity line만 확인됨. 최신 빌드를 직접 열어 확인해야 함. 이미 있으면 제품이 아니라 Grok 패치/플러그인 이슈.
- [ ] **한국/아시아 파워유저 밀도** — 영문 소스에 편향. 국내 Claude/Grok 헤비유저가 세션 관제에 돈을 낼지는 미확인.
- [ ] **세션 파일 스키마 안정성** — Grok `plan.json`, Claude JSONL, Codex rollout 로그는 비공식 파싱. 포맷이 바뀌면 어댑터가 깨진다.

### Load-bearing assumptions
- [ ] **“목표+진행이 상태 한 줄보다 가치 있다”** — 틀리면 Claude/Grok 1st party Dashboard로 충분하고 새 서비스는 필요 없다. 신호: 본인이 2주 써 보고 `/goal` 행을 하루에 몇 번 보는지.
- [ ] **1st party가 6–12개월 안에 구조화 목표 보드를 안 넣는다** — Claude가 Agent View 행에 goal condition을 넣으면 벤더 횡단 외의 이유가 사라진다. 신호: Claude/Grok 릴리즈 노트.
- [ ] **초기 사용자는 Grok을 중심으로 여러 세션을 돌린다** — 범용으로 피벗했으므로 이 가정은 약해짐. 대신 “파워유저가 2개 이상 벤더를 동시에 돌린다”가 로드베어링. 신호: 본인 워크플로의 벤더 수, Conductor/CCManager 사용자 인터뷰.
- [ ] **에이전트가 MCP ingest를 자발적으로 호출한다** — 범용의 핵심 가정. 틀리면 ACP/파일 어댑터가 있는 벤더만 보이고 “모든 에이전트”가 공허해진다. 신호: 설치 후 첫 주에 MCP 경로로 들어오는 세션 비율.

### Risks
- [ ] **1st-party 잠식 (확률 높음)** — Agent View가 이미 #26394를 닫아버리는 쪽으로 갔다. 조기 경보: Grok Dashboard에 goal/todo 컬럼이 생기는 날.
- [ ] **Bloop와 같은 “별은 많은데 회사는 접힘” (중간)** — 관제는 사랑받지만 지불 의향이 낮음. 경보: Conductor가 로컬을 계속 무료로 두고 cloud에서만 돈 받는 구조가 유지되는지.
- [ ] **세션 데이터 파싱이 깨짐 (중간)** — 비공식 파일 포맷. 경보: Grok/Claude 메이저 업데이트 후 대시보드 공백.
- [ ] **조율 없는 병렬이 사용자 신뢰를 깎음** — 대시보드만 있고 충돌 방지/검증이 없으면 “보여 주기만 하는 도구”. 경보: 같은 파일 동시 수정 사고.

### Single-source / paywalled claims
- [ ] **TBRC $1.97B / $2.69B / $9.26B** — 단일 분석사. Grand View / Gartner 원문으로 재검증.
- [ ] **Gartner “2028년 observability가 GenAI 배포의 50%”** — MarkTechPost 2차 인용. [원문](https://www.gartner.com/en/newsroom/press-releases/2026-03-30-gartner-predicts-by-2028-explainable-ai-will-drive-llm-observability-investments-to-50-percent-for-secure-genai-deployment)은 봇 차단. 이 숫자는 Layer A 이야기이지 Layer B가 아님.
- [ ] **Claude Pulse “시간당 20회 확인 → 하루 50–80분”** — 자사 블로그 산수. 독립 측정 없음.
- [ ] **Langfuse “12M SDK downloads/month”** — Confident AI 글의 단일 인용. PyPI로 재확인 필요.

### Timing / staleness
- [ ] **Vibe Kanban / Bloop 상태 (2026-04 셧다운)** — 2026 Q3에 커뮤니티 포크·클라우드 논의가 어떻게 됐는지 재확인.
- [ ] **Conductor Series A (2026-03-30)와 pricing ($50/$60)** — 클라우드 usage 과금이 “not right now”에서 바뀌는지. [pricing](https://www.conductor.build/pricing)
- [ ] **Claude Agent View는 research preview** — 인터페이스·단축키가 바뀐다고 문서에 명시. 2026 Q4 재확인.
- [ ] **LangSmith 가격 단위가 LCU/LSU로 재편** — 블로그들의 “$2.50/1k traces”는 구모델일 수 있음. 2026-08 공식 페이지 기준 seat + LCU/LSU. 분기마다 [langchain.com/pricing](https://www.langchain.com/pricing) 재확인.
- [ ] **Re-verify market dynamics in Korea via local sources** (클리앙/OKKY/디스콰이엇/커리어리, 국내 Claude/Grok 커뮤니티).

## Sources

### Competitors / product
- https://code.claude.com/docs/en/agent-view
- https://code.claude.com/docs/en/goal
- https://github.com/smtg-ai/claude-squad
- https://github.com/kbwo/ccmanager
- https://github.com/androsovm/clorch
- https://github.com/BloopAI/vibe-kanban
- https://github.com/gastownhall/gastown
- https://www.conductor.build
- https://www.conductor.build/pricing
- https://www.ycombinator.com/companies/conductor
- https://nimbalyst.com/blog/best-multi-agent-coding-tools-2026/
- https://addyosmani.com/blog/code-agent-orchestra/
- https://claudepulse.app/blog/managing-multiple-claude-sessions
- https://code.visualstudio.com/blogs/2026/02/05/multi-agent-development
- https://github.com/anthropics/claude-code/issues/26394
- Grok user guide: `~/.grok/docs/user-guide/17-sessions.md`, `23-dashboard.md`, `04-slash-commands.md`

### Market size / industry
- https://www.thebusinessresearchcompany.com/report/large-language-model-llm-observability-platform-global-market-report
- https://www.langchain.com/state-of-agent-engineering
- https://www.marktechpost.com/2026/08/09/top-llm-observability-and-evaluation-platforms-in-2026-langfuse-langsmith-braintrust-arize-and-more-compared/
- https://clickhouse.com/blog/clickhouse-acquires-langfuse-open-source-llm-observability
- https://www.reuters.com/technology/database-management-firm-clickhouse-valued-15-billion-amid-ai-boom-2026-01-16/

### Pricing
- https://langfuse.com/pricing
- https://www.langchain.com/pricing
- https://www.conductor.build/pricing
- https://www.finout.io/blog/claude-code-pricing-2026

### User voice
- https://www.ksred.com/managing-multiple-claude-code-sessions-building-a-real-time-dashboard/
- https://www.reddit.com/r/ClaudeCode/comments/1ux4z17/how_do_you_track_multiple_claude_code_sessions/
- https://www.linkedin.com/posts/michil_i-run-10-claude-code-sessions-at-once-activity-7432874482387734530-Tfna
- https://claudepulse.app/blog/managing-multiple-claude-sessions
