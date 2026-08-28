# Thinkroom｜AI智庫
## Product Concept & Design Philosophy

> **一句話定位：**
>
> 把一個重要問題丟進 Thinkroom，不只得到一個答案，而是讓一個臨時 AI 智庫從不同角度研究、質疑、驗證，最後給出有證據、有取捨的綜合建議。

---

# 1. 為什麼會有 Thinkroom

現在多數 AI Agent 的典型互動模式仍然是：

```text
User asks a question
        ↓
Agent forms one interpretation
        ↓
Agent follows one reasoning path
        ↓
Agent produces one answer
```

即使模型本身很強，這種模式仍然有一個天然限制：

> **一旦早期假設或方向選錯，後續推理很容易沿著同一條路徑持續深化。**

在人類世界裡，重要問題通常不是這樣處理。

真正重要的決策往往會經歷：

```text
個體思考
    ↓
提出不同觀點
    ↓
討論與辯論
    ↓
實驗與證據
    ↓
比較不同解釋
    ↓
形成更好的方案
    ↓
重新思考
```

Thinkroom 想把這種「智庫式思考」變成 AI 可重複使用的能力。

---

# 2. Thinkroom 不是「更多 Agent」

Thinkroom 的核心不是建立很多永久存在的 AI Agent。

我們更想做的是：

> **針對一個問題，暫時 fork 出多條不同的 idea / hypothesis / approach branch。**

每一條 branch 可以：

- 採用不同假設
- 從不同角度分析
- 使用不同研究方法
- 做不同實驗
- 蒐集不同證據
- 對其他 branch 提出 critique

最後再把有價值的結果整合回來。

也就是：

```text
Question
   │
   ▼
Problem Framing
   │
   ├── Perspective A
   ├── Perspective B
   ├── Perspective C
   └── Perspective D
           │
           ▼
        Research
           │
           ▼
        Evidence
           │
           ▼
       Cross-Critique
           │
           ▼
        Synthesis
           │
           ▼
     Recommendation
```

---

# 3. Product Mental Model

對使用者來說，不需要理解：

- RLM
- rollout
- trajectory
- branch controller
- meta-controller
- multi-agent orchestration

對外只需要理解四件事：

```text
Questions
   ↓
Perspectives
   ↓
Evidence
   ↓
Recommendation
```

Thinkroom 的產品體驗應該像：

> **「我有一個重要問題，叫 AI 智庫幫我研究一下。」**

---

# 4. 核心設計哲學

## 4.1 不要急著給唯一答案

Thinkroom 不應該把第一個合理想法直接當成答案。

它應該先問：

> 還有哪些合理但彼此不同的解釋或方案？

## 4.2 不同 branch 應先獨立思考

為了降低 anchoring：

```text
A 不先看 B
B 不先看 C
C 不先看 A
```

每個 branch 先獨立形成：

- hypothesis
- assumptions
- method
- evidence
- conclusion

之後才進入 cross-critique。

## 4.3 不要為了分支而分支

Thinkroom 不應該預設：

> 「一定要提出五種不同答案。」

如果現有方案本身已經合理，也應允許結果是：

```text
KEEP
```

而不是硬做創意。

## 4.4 Evidence 比辯論輸贏重要

Thinkroom 的目標不是模擬幾個 AI 人格互相嘴砲。

真正的流程應該是：

```text
Independent research
       ↓
Competing hypotheses
       ↓
Evidence
       ↓
Critique
       ↓
Synthesis
```

## 4.5 不一定 Winner Takes All

很多重要問題不是 A 全對、B 全錯。

更常見的是：

- A 提供核心架構
- B 找到 A 的風險
- C 提供驗證方法
- D 找到適用邊界

最後：

```text
Solution E = synthesis(A, B, C, D)
```

因此 Thinkroom 的核心 primitive 必須包含 **MERGE**，而不只是 SELECT WINNER。

## 4.6 Optionality First

Thinkroom 不應該把以下能力寫死：

- domain
- model
- agent harness
- evaluator
- experiment tool
- branch strategy

核心設計哲學是：

> **Everything should be optional and composable.**

---

# 5. 核心 Research Loop

```text
FRAME
  ↓
FORK
  ↓
ROLLOUT
  ↓
EVIDENCE
  ↓
CRITIQUE
  ↓
EVALUATE
  ↓
SYNTHESIZE
```

---

# 6. 核心 Primitive

## 6.1 FRAME

先重新定義真正的問題。

例如使用者問：

> 「這個 architecture 要不要重構？」

Thinkroom 可能先拆成：

```text
核心痛點是什麼？
哪些 constraint 不能動？
成功標準是什麼？
最小修改方案是否已足夠？
哪些問題是 implementation，哪些是 architecture？
```

## 6.2 FORK

產生真正有差異的研究方向。

例如：

```text
Branch A — Minimal change
Branch B — Modular refactor
Branch C — Event-driven redesign
Branch D — First-principles redesign
```

## 6.3 ROLLOUT

每條 branch 可以自由：

- reasoning
- web research
- code inspection
- simulation
- backtest
- benchmark
- experiment
- external tool usage

## 6.4 EVIDENCE

每條 branch 必須留下：

```text
支持證據
反對證據
假設
未知項目
適用條件
失效條件
```

## 6.5 CRITIQUE

第二輪才讓各 branch 互相看，目的不是投票，而是找：

- blind spots
- unsupported assumptions
- missing evidence
- hidden trade-offs

## 6.6 EVALUATE

不同 domain 可以有不同 evaluator。

Coding：

```text
correctness
complexity
performance
migration cost
maintainability
```

Trading：

```text
out-of-sample validity
drawdown
transaction cost
regime robustness
factor exposure
```

## 6.7 SYNTHESIZE

最後輸出：

```text
Recommendation
Why
Alternative
Trade-offs
Evidence
Risks
Unknowns
Next experiment
```

---

# 7. General Architecture

Thinkroom 核心不應該知道「這是程式」還是「這是股票」。

它只認識：

```yaml
ResearchProblem:
  question:
  context:
  constraints:
  objectives:
  decision_type:

Branch:
  hypothesis:
  assumptions:
  method:
  evidence:
  artifacts:
  findings:
  confidence:

Evaluation:
  criteria:
  findings:
  falsifiers:
  risks:

Synthesis:
  recommendation:
  alternatives:
  tradeoffs:
  unresolved_questions:
```

---

# 8. Architecture Overview

```text
                      User / Main Agent
                              │
                              ▼
                     ┌────────────────┐
                     │   Thinkroom    │
                     │ Research Core  │
                     └───────┬────────┘
                             │
               ┌─────────────┼─────────────┐
               ▼             ▼             ▼
          Domain Pack    Branch Strategy   Evaluator
               │             │             │
               └─────────────┼─────────────┘
                             ▼
                       Rollout Engine
                             │
               ┌─────────────┼─────────────┐
               ▼             ▼             ▼
           Prime Agent    Direct LLM    Custom RLM
               │
               ▼
       Tools / Experiments / Data
```

---

# 9. Domain Pack

Thinkroom 的核心 generic，domain knowledge 由 Domain Pack 提供。

```text
domains/
    generic/
    coding/
    trading/
```

每個 Domain Pack 可以實作：

```python
class DomainPack:

    def frame_problem(self, request):
        ...

    def collect_context(self):
        ...

    def propose_branch_strategies(self):
        ...

    def build_experiment(self, branch):
        ...

    def evaluate_evidence(self, branch):
        ...

    def synthesize(self, branches):
        ...
```

---

# 10. Killer Use Case #1：Coding

Coding 是 Thinkroom 很適合的第一個 killer use case。

## 10.1 Idea / Architecture Research

例如：

> 「Realtime WebSocket event pipeline 應該怎麼設計？」

Thinkroom 可以 fork：

```text
A — current architecture + tuning
B — Event Bus
C — Actor Model
D — Async Queue Pipeline
E — redesign abstraction boundary
```

每條 branch 可以：

- 讀 repo
- 查 dependency
- 建 prototype
- 跑 benchmark
- 看 tests
- 評估 migration cost

## 10.2 Architecture Review

輸入：

```text
repo
current architecture
pain points
constraints
```

輸出：

```text
KEEP
TUNE
REFACTOR
REDESIGN
NEED_MORE_EVIDENCE
```

## 10.3 Refactor Advisor

例如：

```text
A — minimal refactor
B — module boundary refactor
C — dependency inversion
D — event-driven redesign
E — partial rewrite
```

最後不是直接大改，而是回答哪一條路最值得做，以及為什麼。

## 10.4 Coding Experiment Tools

```text
git worktree
compiler
unit tests
integration tests
benchmark
static analysis
dependency graph
```

---

# 11. Killer Use Case #2：Trading / Investment

Trading / Investment 是第二個刻意選來驗證 generality 的 domain。

原因：

> Coding 與 Trading 的 evidence structure 完全不同。

如果同一套 Research Core 能支援兩者，代表 abstraction 比較可能是正確的。

## 11.1 Investment Thesis Research

例如：

> 「AI Server 族群目前還值得追嗎？」

可以 fork：

```text
H1 — secular growth remains intact
H2 — valuation already discounts growth
H3 — current move is liquidity-driven
H4 — supply-chain bottleneck changes winners
H5 — macro regime overrides sector fundamentals
```

## 11.2 Strategy Diagnosis

例如：

> 「這個策略最近 performance degradation 是什麼原因？」

可以 fork：

```text
H1 — temporary noise
H2 — market regime change
H3 — universe selection drift
H4 — execution / slippage
H5 — signal decay
```

## 11.3 Trading Experiments

```text
historical backtest
walk-forward
out-of-sample test
regime split
Monte Carlo
stress test
transaction-cost simulation
factor decomposition
```

## 11.4 Trading Evaluator

不能只比較 return，還必須考慮：

```text
evidence quality
out-of-sample validity
robustness
drawdown
regime dependency
execution cost
alternative explanations
```

這反而能逼 Thinkroom 的核心變成真正的 **Epistemic Research Engine**，而不只是 candidate comparison engine。

---

# 12. Branch Strategy 也是 Plug-in

不同問題需要不同 fork 方法。

例如：

```text
architecture-diversity
competing-causal-hypotheses
orthogonal-perspectives
red-team
contrarian
minimal-change
first-principles
historical-analogy
conservative
aggressive
```

因此整個系統可以視為：

```text
Research Engine
    ×
Domain Pack
    ×
Branch Strategy
    ×
Rollout Backend
    ×
Evaluator
```

---

# 13. Rollout Backend 應可替換

Thinkroom 不應綁死任何一家模型或 Agent。

```text
backends/
    prime_agent/
    openai/
    anthropic/
    local/
    custom_rlm/
```

第一版可以使用成熟 Agent runtime 當 backend，之後再視需求自行實作更輕量的 RLM runtime。

---

# 14. Soft / Experiment / Persistent Branch

不是每個研究方向都值得啟動完整 Agent。

## Soft Branch

只做 hypothesis research，不需要 workspace，成本最低。

## Experiment Branch

需要實驗時才建立 isolated environment / workspace / data / tests / simulation。

跑完留下：

```text
artifact
evidence
result
```

## Persistent Branch

只有真的需要長期研究時才升級：

```text
persistent context
persistent workspace
multiple follow-ups
```

---

# 15. Thinkroom 不等於 Multi-Agent Chat Room

產品不應該長成：

```text
AI A：我認為...
AI B：我不同意...
AI C：兩位都說得很好...
```

Thinkroom 更偏向：

```text
Independent Rollout
        ↓
Evidence
        ↓
Cross-Critique
        ↓
Synthesis
```

> **研究優先，角色扮演其次。**

---

# 16. 對外產品介面

對使用者可以提供非常簡單的入口：

```text
Research an Idea
Review a Design
Challenge a Thesis
Compare Approaches
```

---

# 17. CLI

```bash
thinkroom research   --question "How should we redesign the event pipeline?"
```

Coding：

```bash
thinkroom code architecture .
```

Refactor：

```bash
thinkroom code refactor src/websocket/
```

Trading：

```bash
thinkroom trade research thesis.yaml
```

---

# 18. MCP

MCP 很適合做成通用 tool interface：

```text
thinkroom.research
thinkroom.review_design
thinkroom.challenge_thesis
thinkroom.compare
```

主 Agent 可以在需要 second opinion 時呼叫。

---

# 19. Python SDK

```python
from thinkroom import Thinkroom

room = Thinkroom(domain="coding")

result = room.research(
    question="Should we redesign the event pipeline?",
    context="./repo"
)
```

---

# 20. 專案結構

```text
thinkroom/
    core/
        research_engine
        branch_manager
        rollout_manager
        evidence_store
        critic
        synthesizer

    domains/
        generic/
        coding/
        trading/

    strategies/
        first_principles/
        contrarian/
        red_team/
        architecture_diversity/
        competing_hypotheses/

    evaluators/
        generic/
        coding/
        trading/

    backends/
        prime_agent/
        openai/
        anthropic/
        local/
        custom_rlm/

    interfaces/
        cli/
        mcp/
        python_sdk/
```

---

# 21. MVP

第一版不要追求完整 AI 文明。

只驗證：

> **多 branch 研究 + evidence synthesis，是否比單一路徑回答更有價值？**

MVP Core：

```text
FRAME
FORK
ROLLOUT
CRITIQUE
SYNTHESIZE
```

MVP Domain 至少同時保留：

```text
Coding
Trading / Investment
```

MVP Interface：

```text
CLI
Python SDK
MCP
```

---

# 22. 第一版先不要處理

- 自動監控 Agent 是否鑽牛角尖
- 自動 interrupt 主 Agent
- 自動修改 production code
- 自動交易執行
- 長期 autonomous self-improvement
- learned meta-controller
- 大型 persistent multi-agent society

這些都可以是未來延伸。

---

# 23. Future：從 Advisor 到 Autonomous Research System

第一階段：

```text
Human / Agent explicitly asks:
"研究一下這個問題"
```

未來可以逐步演進成：

```text
Agent trajectory
      ↓
detect uncertainty / disagreement
      ↓
automatically open Thinkroom
      ↓
research branches
      ↓
return recommendation
```

再往後才可能變成：

```text
continuous research
shared memory
persistent hypotheses
autonomous experiments
knowledge accumulation
```

也就是逐漸接近：

> **machine research institution**

甚至：

> **AI civilization-like knowledge evolution**

但這不是 MVP 的前提。

---

# 24. Thinkroom 與「文明式 AI 演進」

Thinkroom 的長期靈感來自一個觀察：

人類文明的進步，不只是個體 introspection。

更像：

```text
Introspection
     ↓
Communication
     ↓
Debate
     ↓
Experiment
     ↓
Evidence
     ↓
Shared Knowledge
     ↓
Internalization
     ↓
Introspection
     ↺
```

Thinkroom 想把其中最小可工程化的一部分做出來：

```text
Independent Thought
        ↓
Competing Ideas
        ↓
Evidence
        ↓
Critique
        ↓
Synthesis
```

而不需要真的啟動一大群永久 Agent。

---

# 25. Branches as Temporary Minds

> **不是 civilization of agents，而是 civilization of trajectories。**

每一條 branch 可以被視為一個暫時存在的「思想個體」。

它可以：

- 有自己的假設
- 有自己的研究方向
- 犯錯
- 提出反對意見
- 做實驗
- 被淘汰

branch 死掉沒有關係。

留下來的是：

```text
evidence
insight
artifact
new hypothesis
falsified assumption
```

這些才會被 merge 回 shared knowledge。

---

# 26. Cognitive Git

Thinkroom 的底層概念也可以類比成：

> **Git for machine cognition**

```text
人類文明             Thinkroom

人                   Branch
觀點                 Hypothesis
論文                 Research Result
實驗                 Experiment
證據                 Evidence
引用                 Dependency
辯論                 Cross-Critique
學派                 Persistent Branch
科學共識             Merge
錯誤理論             Pruned Branch
重新研究舊理論       Checkout old branch
```

這個比喻適合內部工程思考，對外產品不需要使用。

---

# 27. 核心護欄

## 27.1 不把 Confidence 當 Evidence

```text
"I am 90% confident"
```

不代表有證據。

## 27.2 保留反例

每個 branch 都應該留下：

```text
supporting evidence
contradicting evidence
falsifiers
```

## 27.3 保留 Uncertainty

Thinkroom 應允許：

```text
NEED_MORE_EVIDENCE
```

而不是永遠硬給答案。

## 27.4 Domain Safety

例如 Trading：

Thinkroom 第一階段應定位為：

> Research / Decision Support

而不是：

> Autonomous Execution

---

# 28. Product Positioning

## 中文

### Thinkroom｜AI智庫

> **有重要問題？叫 AI 智庫幫你研究。**

## 英文

### Thinkroom

> **A think tank for any question.**

或：

> **Bring more perspectives to important decisions.**

或：

> **Research the alternatives before choosing the answer.**

---

# 29. Product Vocabulary

對外：

```text
Question
Perspective
Research
Evidence
Challenge
Recommendation
```

對內：

```text
Frame
Fork
Rollout
Critique
Evaluate
Merge
```

避免對一般使用者直接暴露：

```text
RLM
trajectory
meta-controller
epistemic branching
```

除非是 developer mode。

---

# 30. Thinkroom 最重要的產品原則

> **不是打造一個什麼都會的 Agent。**

而是：

> **提供一套 primitive，讓任何 Agent 或任何人，在面對重要且不確定的問題時，可以探索多個可能世界，再帶著證據回來做決定。**

---

# 31. 一句話版本

> **Thinkroom 是一個可以隨叫隨到的 AI 智庫：它不急著回答，而是先替你把不同想法研究過一遍。**

---

# 32. Internal Design Mantra

> **Don’t just generate another answer.
>
> Create another line of inquiry.**

中文：

> **不要只是多生一個答案，而是多開一條值得研究的路。**
