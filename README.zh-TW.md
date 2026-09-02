# Thinkroom｜AI 智庫

[English](README.md) · **繁體中文**

> **不要只是多生一個答案，而是多開一條值得研究的路。**

Thinkroom 是一個隨叫隨到的 AI 智庫，專門處理那些不能只靠模型單次回答就下結論的重要問題。它會把一個高後果問題分成多條彼此獨立的研究路線，用證據檢驗，再匯成一份可追溯的建議；取捨、不確定性與異議不會被藏在結論後面。

## 一個好答案，為什麼還不夠

多數 AI 互動只走一條路：

```text
問題 → 一種解讀 → 一條推理路徑 → 一個答案
```

這條路可以非常流暢，卻仍然脆弱。只要一開始的假設、問題框架或因果故事選錯，後續每一步都可能只是把同一個錯誤講得更完整。

人類面對重要決策時，通常不會只找一個人一路想到底。我們會讓不同觀點先各自研究、比較證據、挑戰隱藏假設、做實驗，最後再判斷哪些部分站得住腳。Thinkroom 把這種智庫式思考，做成任何人或 Agent 都能重複使用的最小工程單位。

```text
問題
  │
  ▼
重新框定真正要做的決策
  │
  ├── 觀點 A ── 獨立研究 ── 證據
  ├── 觀點 B ── 獨立研究 ── 證據
  └── 觀點 C ── 獨立研究 ── 證據
                          │
                          ▼
                    延後交叉批判
                          │
                          ▼
                建議 + 異議 + 未知項目
```

每個分支都是暫時存在的「思想個體」，不是永久 Agent 人設。它們拿到相同且有界的共同脈絡，各自形成假設，記錄支持與反對證據，並在看到其他分支之前先提交自己的結果。等所有觀點定型後，交叉批判才開始。

## 它不是 Multi-Agent 聊天室

Thinkroom 的目標，不是產出一段 AI 角色輪流同意、反對的對話紀錄。真正有價值的單位，是一條被研究過的 inquiry。

- **先獨立，再討論。** 分支在形成觀點時看不到彼此，不會過早錨定或互相模仿。
- **證據高於信心。** 「我有九成把握」不等於有證據；來源與驗證狀態會跟著結果一起留下。
- **先提交，再批判。** 每個分支先暴露自己的假設與可反駁條件，辯論才有意義。
- **先想怎麼合併，不急著選贏家。** 最好的建議可能採用 A 的架構、B 找到的風險，以及 C 提出的驗證方法。
- **允許誠實地不知道。** 當證據還不足以支撐決策時，`NEED_MORE_EVIDENCE` 比硬給答案更有價值。

對使用者來說，心智模型仍然很簡單：

```text
問題 → 觀點 → 證據 → 建議
```

底層則是一個有紀律的研究循環：

```text
FRAME → FORK → ISOLATED ROLLOUTS → EVIDENCE → DELAYED CRITIQUE → SYNTHESIZE
```

## 一個具體例子

假設你問：「這個 event pipeline 應不應該重做？」單一 Agent 很可能很快就愛上某一種架構。Thinkroom 會先開出幾條真正不同的研究路線：

- 保留現有設計，只排除已量測到的瓶頸；
- 做有邊界的模組化重構；
- 改用 event bus 或 actor model；
- 反過來檢查：目前問題真的屬於架構層嗎？

每條路線都可以讀程式碼、跑測試或 benchmark、估算 migration cost，並說清楚什麼證據會推翻自己的結論。最後不一定要選一個冠軍；合理的綜合方案可能保留現有核心、採用重構方案的一條邊界，再把反方分支提出的實驗當成 rollout gate。

同一個研究核心也能支援投資論點、事故診斷、產品策略、政策取捨或 due diligence，因為 domain knowledge、branch strategy、evaluator 與 rollout backend 都保持可替換、可組合。

## 什麼時候適合用 Thinkroom

以下四個條件都成立時，很適合啟動 Thinkroom：

- 結果足夠重要，草率回答可能帶來明顯成本或難以回復的後果；
- 答案真的不確定、有爭議，或同時存在多個競爭假設；
- 技術、產品、營運、反方或領域觀點若先獨立研究，能有效區分這些假設；
- 最後需要的是可稽核的建議，而不是看完即丟的聊天回答。

若只是簡單查詢、確定性計算、單純改寫、直接工具操作，或一個權威來源就能回答的問題，不必啟動 Thinkroom。分支只有在創造真正不同的研究路線時才有價值，不是越多越好。

## 最後會帶回什麼

一個完成的研究工作，應該能清楚回答：

- **建議：**目前證據最支持什麼；
- **理由：**背後的推理與已驗證證據；
- **替代方案：**仍然合理、可以保留的其他路徑；
- **取捨：**每條路得到什麼，又犧牲什麼；
- **風險與可反駁條件：**哪些新證據會讓建議失效；
- **未知與異議：**綜合結果無法誠實解決的部分；
- **下一個實驗：**降低剩餘不確定性的最低成本方法。

證據、來源、驗證狀態、保留的異議與 job state，都會一路連回最終綜合結果。

## 內含功能

| 介面 | 你可以做什麼 |
| --- | --- |
| Web UI | 送出問題並查看證據豐富的研究結果。 |
| CLI | 研究、查詢、列出、取消、啟動服務及執行 MCP。 |
| Python SDK | 使用遠端 `ThinkroomClient` 或嵌入式 `Thinkroom`。 |
| REST API | 以穩定的 job 介面整合，並支援冪等請求。 |
| MCP | `thinkroom_research`、`thinkroom_get_research`、`thinkroom_list_research` 與 `thinkroom_cancel_research`。 |
| Agent Skills | 提供安裝、觸發判斷與操作指引。 |
| Backends | 以 typed ports 隔離 deterministic `scripted`、OpenAI-compatible 與 Prime Agent adapters。 |

Thinkroom 刻意維持 local-first、單節點設計。它可以接進既有 Agent stack，但不接管 Agent、模型供應商或最後的決策權。

## 從原始碼快速開始

需求：Python 3.12+ 與 [`uv`](https://docs.astral.sh/uv/)。

```bash
uv lock --check
uv sync --locked --all-extras --dev
install -d -m 0700 .data
uv run thinkroom serve
```

另開一個終端機：

```bash
uv run thinkroom research \
  --question "Should we adopt this design?" \
  --idempotency-key demo-001
```

服務預設監聽 `127.0.0.1:8787`，並使用 deterministic `scripted` backend。

## 安裝到 Codex App 或 Hermes Agent

Thinkroom 內含三個受管理的 Agent Skills：

| Skill | 適合載入的時機 |
| --- | --- |
| `thinkroom-trigger` | 判斷一個重要、不確定、含多個競爭假設的問題是否值得啟動 Thinkroom；瑣碎或確定性工作不要載入。 |
| `thinkroom-operate` | 透過 CLI、REST API 或 MCP 送出、輪詢、檢查、取消或解讀 Thinkroom 工作。 |
| `thinkroom-install` | 安裝、檢查或移除受管理的 Thinkroom skill 投影。 |

Runtime 與 skill bundle 共用同一份權威；只有 host registration 分流。請依 agent profile 安裝並驗證同一份受管理 bundle：

```bash
# Codex App / CLI / IDE
uv run thinkroom skills install --profile codex
uv run thinkroom skills status --profile codex

# Hermes Agent default profile
unset HERMES_HOME
uv run thinkroom skills install --profile hermes
uv run thinkroom skills status --profile hermes
```

Codex profile 解析到 `$HOME/.agents/skills`；Hermes profile 解析到
`$HERMES_HOME/skills`，未設定時預設為 `~/.hermes/skills`。其他相容host可使用
`--target <absolute-skill-root>`。安裝器具備冪等性，而且不會覆寫未受管理或已分歧的檔案。

## 透過 MCP 連接

先啟動服務，再將同一個 Thinkroom stdio MCP server註冊到所選host。

Codex App／CLI／IDE：

```bash
codex mcp add thinkroom \
  --env THINKROOM_ENDPOINT=http://127.0.0.1:8787 \
  -- /absolute/path/to/thinkroom mcp
codex mcp list
```

Hermes Agent default profile：

```bash
hermes --profile default mcp add thinkroom \
  --command /absolute/path/to/thinkroom \
  --env THINKROOM_ENDPOINT=http://127.0.0.1:8787 \
  --args mcp
hermes --profile default mcp test thinkroom
```

請在互動提示中啟用所發現的四個Thinkroom tools；接著開啟新的Hermes session或重新載入MCP discovery。
Named profile必須在每個Thinkroom Skills命令前設定
`HERMES_HOME="$HOME/.hermes/profiles/<profile-name>"`，並將上述`default`換成同一個
Hermes profile名稱。

Windows上的正式Codex App profile會讓agent與Thinkroom都在WSL2執行。Windows App與WSL CLI
預設使用不同的Codex home，因此必須明確選擇共享MCP設定。完整的Codex Windows/WSL與Hermes
profile步驟請見[安裝與Agent整合](docs/INSTALLATION.md)。

## Provider backends

預設的 `scripted` backend 只能證明編排機制正常，不能證明模型研究品質。若要接 provider，可設定：

```bash
export THINKROOM_BACKEND=openai
export THINKROOM_OPENAI_API_KEY=...
```

或：

```bash
export THINKROOM_BACKEND=prime_agent
export THINKROOM_PRIME_AGENT_EXECUTABLE=/absolute/path/to/prime-agent
export THINKROOM_PRIME_AGENT_PROVIDER=openai-codex
export THINKROOM_PRIME_AGENT_MODEL=gpt-5.6-luna
export THINKROOM_PRIME_AGENT_THINKING=max
export THINKROOM_MAX_CONCURRENCY=1
export THINKROOM_ROLLOUT_PROVIDER_CONCURRENCY=1
export THINKROOM_JOB_SOFT_TIMEOUT_SECONDS=900
export THINKROOM_BACKEND_TIMEOUT_SECONDS=180
export THINKROOM_JOB_TIMEOUT_SECONDS=1200
```

若要在主要 route 不可用時做一次循序 fallback，請使用同一個 executable 並明確設定兩條 route：

```bash
export THINKROOM_BACKEND=prime_agent_failover
export THINKROOM_PRIME_AGENT_EXECUTABLE=/absolute/path/to/prime-agent
export THINKROOM_PRIME_AGENT_PROVIDER=openrouter
export THINKROOM_PRIME_AGENT_MODEL=z-ai/glm-5.3-flash
export THINKROOM_PRIME_AGENT_THINKING=high
export THINKROOM_PRIME_AGENT_FALLBACK_PROVIDER=openai-codex
export THINKROOM_PRIME_AGENT_FALLBACK_MODEL=gpt-5.6-terra
export THINKROOM_PRIME_AGENT_FALLBACK_THINKING=high
export THINKROOM_FAILOVER_PRIMARY_TIMEOUT_SECONDS=90
export THINKROOM_BACKEND_TIMEOUT_SECONDS=180
export THINKROOM_JOB_SOFT_TIMEOUT_SECONDS=900
export THINKROOM_JOB_TIMEOUT_SECONDS=1200
```

只有在 provider error 於 fast-transient 門檻內結束時，才會在同一 route 重試一次；timeout
會直接跳過重試。重試、fallback 與保留原 route 的 schema repair 共用每個 phase/branch 三次
實體呼叫預算。取消、fencing、耗盡的 deadline 與 semantic/result output limit 都不會跨 route
放大。唯一例外是 primary raw-transport output limit：它不重試 primary，會開啟 attempt-local
primary circuit，並可在同一預算內使用一次已設定的 fallback。
完整 deadline、circuit、partial-result 與 concurrency contract 請見
[Provider resilience and progress](docs/provider-resilience-v0.2.5.md)。

請先透過 [Prime Agent](https://github.com/PrimeIntellect-ai/prime-agent) 的互動式 `/login`
流程完成認證。Thinkroom 不會複製 OAuth 憑證；
Prime Agent 仍自行讀取與更新它擁有的 credential store。每次 Thinkroom provider invocation
會建立一個有界的 RPC session，要求一個原生 RLM child，且只有在同一 session 收到名稱
相符的 child `agent_message` 後，才接受 schema JSON。暫存 working/session directory 會在
process 完整 settle 後清除。
上述 concurrency 與 timeout 是 root-plus-child model work 的保守起點，不是通用容量承諾；
應依實際 provider latency 與 quota 調整。

完整 runtime contract 與環境變數請見 [Operations](docs/OPERATIONS.md)。

## 冪等工作

長時間執行的提交會先回傳 job handle。REST caller 使用 `Idempotency-Key`；Python SDK 使用 `ThinkroomClient.research(..., idempotency_key="demo-001")`；CLI 使用 `--idempotency-key`；MCP 則在 `thinkroom_research` 公開 `idempotency_key`。

若同一個 key 搭配不同 request 重複使用，系統會以 `IDEMPOTENCY_CONFLICT` 拒絕。

## 經 release 授權的正式安裝方式

正式安裝時，wheel、`requirements-production.txt`、`uv.lock` 與 `verify_locked_runtime.py` 必須來自**同一個 GitHub Release**。先安裝精確鎖定的 dependency closure，再以不解析相依套件的方式安裝 wheel：

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-production.txt
uv pip install --python .venv/bin/python --no-deps thinkroom-0.2.5-py3-none-any.whl
.venv/bin/python verify_locked_runtime.py uv.lock --write-manifest runtime-lock-manifest.json
```

一般會自動解析相依套件的 wheel 安裝方式適合評估，但不屬於 locked production closure。經 release 授權的部署方式，是讓已安裝的 Python package 以 native POSIX process 在 Linux 執行；Windows 則在 WSL 內執行。

## 維運與安全邊界

Thinkroom v0.2 刻意維持單節點架構：

- production 預設只綁定 literal loopback；
- SQLite 只支援一個 service instance；
- database parent 必須事先存在於 Linux/POSIX filesystem，擁有者須為有效 service user（或 root），且不可讓 group/world 寫入；
- v0.2 沒有 authentication、RBAC 或 multi-tenancy；
- 若要開放更廣的存取範圍，必須另設安全且經過驗證的 authenticated reverse proxy。

Docker 是由 operator 自行負責的參考資料，不屬於 v0.2 native release claim。若要使用，必須只發布在 host loopback，例如 `-p 127.0.0.1:8787:8787`，並完成 [Operations](docs/OPERATIONS.md) 中的 hardening checklist。

## 驗證

安裝 wheel 後執行：

```bash
thinkroom verify package
thinkroom verify process
```

Repo contributor 應執行 [AGENTS.md](AGENTS.md) 所列的完整 gates。

## 專案文件

- [產品概念與設計哲學](thinkroom_ai_think_tank_product_concept.md)
- [產品規格](docs/specification.md)
- [維運說明](docs/OPERATIONS.md)
- [安裝與 Agent 整合](docs/INSTALLATION.md)
- [架構決策：有界的 Prime Agent RLM RPC](docs/adr/0003-prime-agent-rlm-rpc.md)
- [安全政策](SECURITY.md)
- [架構決策：modular monolith](docs/adr/0001-modular-monolith.md)
- [架構決策：native process release authority](docs/adr/0002-native-process-release-authority.md)
- [架構決策：Agent host integration profiles](docs/adr/0004-agent-host-integration-profiles.md)

## 授權

Thinkroom 採用 [MIT License](LICENSE) 開源。
