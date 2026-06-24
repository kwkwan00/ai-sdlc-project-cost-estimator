# AI SDLC Cost Estimator — Application Planning Outline

## Digital Twin Agent Orchestration Architecture

---

## 1. System Architecture

### 1.1 Orchestration Layer

- The Orchestrator is the sole user-facing agent — all interaction flows through it
- Manages the two-pass estimation cycle:
  - Pass 1: Distribute raw input to all six twins, collect preliminary assessments and gap analyses
  - Pass 2: Merge user answers with inferences, redistribute full context, collect final estimates
- Responsible for deduplicating and prioritizing clarifying questions across all twins
- Performs cross-phase consistency checking — flags where one twin's assumptions contradict another's
- Assembles the final synthesized deliverable

### 1.2 Twin Agent Design

- Six autonomous reasoning engines, one per AI SDLC phase:
  1. Discovery Analyst
  2. UX/Design Strategist
  3. Development Architect
  4. Code Review Sentinel
  5. Deployment & DevOps Engineer
  6. QA & Testing Strategist
- Each twin is implemented as a LangGraph subgraph (node) within the Orchestrator's StateGraph
- Each twin publishes an A2A Agent Card describing its skills, input/output schemas, and cross-phase dependencies — this makes the twin architecture discoverable, extensible, and compatible with external agents in the future
- Each twin produces a structured output: effort range (low/mid/high), assumptions list, risk flags, and cross-phase dependency notes
- Twins do not interact with the user directly — they communicate only with the Orchestrator and with each other via A2A messages for cross-phase signals

### 1.3 Communication Protocol: A2A (Agent-to-Agent)

The inter-agent communication uses Google's A2A protocol — an open standard (now Linux Foundation, v1.0, 150+ organizations) built on HTTP, JSON-RPC 2.0, and Server-Sent Events. A2A was chosen over custom message schemas because it provides a standardized task lifecycle, streaming support, and forward compatibility with external agent ecosystems.

#### 1.3.1 Why A2A for Internal Twin Communication

- **Standardized task lifecycle:** Every twin interaction is tracked as an A2A Task with states: `submitted` → `working` → `completed` / `failed`. This gives the Orchestrator and the UI layer a consistent model for progress tracking without custom state management
- **Structured message format:** A2A Messages carry Parts (TextPart, DataPart, FilePart) — the DataPart type maps directly to the structured JSON schemas twins need for context packages and estimate outputs
- **Cross-phase signaling without Orchestrator bottleneck:** A2A allows twins to send messages directly to other twins as peers, not just back to the Orchestrator. This enables the reactive parallel execution model described in Section 1.3.3
- **Extensibility:** If a seventh twin is added later (e.g., an AI Ethics/Governance twin), it registers an Agent Card and immediately participates in the protocol — no Orchestrator code changes needed for discovery
- **Future interoperability:** Because A2A is the emerging standard for agent-to-agent communication, twins could eventually communicate with external agents (e.g., a client's own estimation system, a third-party risk assessment agent) without protocol translation

#### 1.3.2 Agent Cards

Each twin and the Orchestrator publish an Agent Card at a well-known endpoint. The Agent Card contains:

```json
{
  "name": "development-architect",
  "description": "Estimates development effort for AI SDLC projects, including tech stack analysis, AI-to-human ratio calculation, and integration complexity assessment",
  "version": "1.0.0",
  "url": "http://localhost:8100/agents/development-architect",
  "skills": [
    {
      "id": "estimate-development-effort",
      "name": "Estimate Development Effort",
      "description": "Produces effort ranges for the development phase based on project context, AI maturity, and technical environment",
      "inputModes": ["application/json"],
      "outputModes": ["application/json"]
    }
  ],
  "defaultInputModes": ["application/json", "text/plain"],
  "defaultOutputModes": ["application/json"],
  "capabilities": {
    "streaming": true,
    "pushNotifications": false
  },
  "crossPhaseSubscriptions": ["discovery-analyst", "ux-design-strategist"],
  "crossPhasePublishes": ["code-review-sentinel", "qa-testing-strategist", "deployment-devops-engineer"]
}
```

**Custom extension — `crossPhaseSubscriptions` / `crossPhasePublishes`:** These are application-specific fields (A2A allows extensions) that declare which twins a given twin listens to and which twins it emits signals to. The Orchestrator reads these at initialization to build the LangGraph edge topology.

#### 1.3.3 Execution Model: Parallel with Reactive A2A Signaling

The system uses a hybrid parallel-reactive model that solves the core tradeoff in the original plan ("parallel is faster, but sequential allows downstream twins to react to upstream outputs"):

**Pass 1 — Full Parallel Fan-Out:**
- All six twins launch simultaneously via LangGraph's static fan-out (six parallel edges from the Orchestrator's "distribute" node)
- Each twin receives the identical context package as an A2A `message/send` with a DataPart containing the structured context
- All six twins run as a single LangGraph superstep — the fan-in merger node waits for all six to complete
- No cross-phase signaling in Pass 1 — twins estimate independently to surface genuine gaps and produce unbiased preliminary assessments
- Pass 1 output: six preliminary estimates + six gap lists → Orchestrator deduplicates into clarifying questions

**Pass 2 — Parallel with Reactive Cross-Phase Signals:**
- All six twins launch simultaneously again (fan-out), but this time they can react to upstream signals mid-execution
- The reactive mechanism works as follows:
  1. Twins that produce cross-phase signals (per their `crossPhasePublishes` declaration) emit A2A messages to subscribing twins as SSE events while still in their `working` state
  2. Subscribing twins listen for these signals and incorporate them before finalizing their own estimates
  3. The Orchestrator does NOT mediate these cross-phase signals — twins communicate peer-to-peer via A2A
- Example flow:
  - Discovery Analyst completes early (typically fastest — less computation) and emits a cross-phase signal: `{"signalType": "discovery-completeness", "rating": "medium", "ambiguityFlags": ["stakeholder count uncertain", "business rules underspecified"]}`
  - UX/Design Strategist and Development Architect receive this signal via A2A SSE and adjust their estimates accordingly (e.g., increase design iteration cycles, widen effort range)
  - Development Architect completes and emits a `development-assessment` signal (see Section 3.3.4 for full schema) containing tech stack classification with per-layer multipliers, code volume estimate broken down by language, AI-to-human ratio per layer, infrastructure leverage scorecard, and modernization pattern if applicable
  - Code Review Sentinel receives the language breakdown and AI ratio to calibrate review effort per language (e.g., Java PRs need more review time than TypeScript PRs, AI-generated code layers need different review patterns)
  - QA/Testing Strategist receives integration count, legacy modernization flags, and language breakdown to scope integration testing, regression testing, and framework-specific test tooling
  - Deployment & DevOps Engineer receives infrastructure leverage data and modernization pattern to assess how much pipeline/deployment work is already covered versus needs building

**Signal Dependency Graph (A2A Cross-Phase Subscriptions):**

```
Discovery Analyst ──────► UX/Design Strategist
       │                         │
       │                         ▼
       └────────────────► Development Architect
                                 │
                    ┌────────────┼────────────┐
                    ▼            ▼            ▼
            Code Review    Deployment    QA/Testing
             Sentinel      & DevOps     Strategist
```

- Discovery Analyst publishes to: UX/Design, Development
- UX/Design Strategist publishes to: Development (design complexity signals)
- Development Architect publishes to: Code Review, Deployment, QA/Testing
- Code Review Sentinel publishes to: Development (kickback rate feedback for re-estimation consideration)
- Deployment & DevOps publishes to: QA/Testing (environment readiness signals)
- QA/Testing Strategist publishes to: (terminal — no downstream consumers)

**Timeout and Fallback Behavior:**
- Each twin has a maximum execution window (configurable, default 30 seconds per pass)
- If a subscribing twin's upstream publisher hasn't emitted a signal within 15 seconds, the subscriber proceeds without the cross-phase signal and states the assumption in its output ("Discovery signal not received — assuming medium completeness")
- This ensures that a slow or failing upstream twin doesn't block the entire estimation pipeline

#### 1.3.4 A2A Message Schemas for Cross-Phase Signals

All cross-phase signals use A2A's DataPart with a standardized envelope:

```json
{
  "role": "agent",
  "parts": [
    {
      "type": "data",
      "mimeType": "application/json",
      "data": {
        "signalType": "discovery-completeness",
        "sourceAgent": "discovery-analyst",
        "targetAgents": ["ux-design-strategist", "development-architect"],
        "timestamp": "2026-05-27T14:30:00Z",
        "payload": {
          "completenessRating": "medium",
          "ambiguityFlags": ["stakeholder count uncertain"],
          "confidenceLevel": 0.65,
          "suggestedAdjustments": {
            "increaseDesignIterations": true,
            "widenEffortRange": 1.2
          }
        }
      }
    }
  ]
}
```

Each twin type defines its own signal payload schema (documented in the twin specifications in Section 3), but all share the common envelope fields: `signalType`, `sourceAgent`, `targetAgents`, `timestamp`, `confidenceLevel`.

### 1.4 LangGraph Implementation Architecture

#### 1.4.1 StateGraph Design

The entire estimation pipeline is a single LangGraph StateGraph with the following structure:

```python
from langgraph.graph import StateGraph, START, END
from typing import Annotated
import operator

class EstimationState(TypedDict):
    # Input from user (Stages 1-3)
    raw_input: str
    uploaded_files: list[str]
    project_context: dict          # Stage 2 structured fields
    role_rates: dict               # Stage 2 rate table {sr_product, jr_product, sr_technical, jr_technical}
    ai_maturity: dict              # Stage 3 maturity ratings per phase (Level 1-5)
    ai_role_effectiveness: dict    # Stage 3 per-phase per-role AI effectiveness overrides {phase: {role: "none"|"low"|"medium"|"high"|"very_high"}}
    role_percentages: dict         # Stage 3 {sr_product: float, jr_product: float, sr_technical: float, jr_technical: float} — must sum to 1.0
    team_profile: dict             # Stage 3 capability factors (domain exp, platform exp, etc.)

    # Pass 1 outputs (reducer: list append for parallel fan-in)
    pass1_estimates: Annotated[list[dict], operator.add]
    pass1_gaps: Annotated[list[dict], operator.add]

    # Clarifying questions (Orchestrator-generated from pass1_gaps)
    clarifying_questions: list[dict]
    user_answers: dict             # Filled after Stage 4

    # Pass 2 outputs (reducer: list append for parallel fan-in)
    pass2_estimates: Annotated[list[dict], operator.add]
    cross_phase_signals: Annotated[list[dict], operator.add]

    # Final output
    synthesized_estimate: dict
    consistency_report: dict
    commercial_adjustments: dict
```

**Key design decisions:**
- `pass1_estimates`, `pass1_gaps`, `pass2_estimates`, and `cross_phase_signals` use the `Annotated[list, operator.add]` reducer pattern so parallel twin nodes can all write to the same state key without data loss — LangGraph merges their lists automatically at the fan-in point
- The state object is the single source of truth throughout the graph — twins read from it and write back to it
- Cross-phase A2A signals are also logged into `cross_phase_signals` for observability, even though they flow peer-to-peer between twins in real time

#### 1.4.2 Graph Topology

```python
graph = StateGraph(EstimationState)

# Nodes
graph.add_node("parse_input", parse_input_node)           # AI parsing pass
graph.add_node("discovery_analyst", discovery_twin)
graph.add_node("ux_design_strategist", ux_design_twin)
graph.add_node("development_architect", development_twin)
graph.add_node("code_review_sentinel", code_review_twin)
graph.add_node("deployment_devops", deployment_twin)
graph.add_node("qa_testing_strategist", qa_testing_twin)
graph.add_node("merge_pass1", merge_pass1_node)            # Fan-in + question generation
graph.add_node("await_user_answers", user_interaction_node) # Stage 4 — human-in-the-loop
graph.add_node("merge_pass2", merge_pass2_node)            # Fan-in
graph.add_node("consistency_check", consistency_check_node)
graph.add_node("commercial_processing", commercial_node)
graph.add_node("synthesize_estimate", synthesis_node)

# Pass 1: fan-out to all six twins in parallel
graph.add_edge(START, "parse_input")
graph.add_edge("parse_input", "discovery_analyst")         # Static fan-out —
graph.add_edge("parse_input", "ux_design_strategist")      # all six edges from
graph.add_edge("parse_input", "development_architect")     # parse_input create
graph.add_edge("parse_input", "code_review_sentinel")      # a single LangGraph
graph.add_edge("parse_input", "deployment_devops")         # superstep where all
graph.add_edge("parse_input", "qa_testing_strategist")     # six twins run in parallel

# Fan-in: all six twins converge at merge_pass1
graph.add_edge("discovery_analyst", "merge_pass1")
graph.add_edge("ux_design_strategist", "merge_pass1")
graph.add_edge("development_architect", "merge_pass1")
graph.add_edge("code_review_sentinel", "merge_pass1")
graph.add_edge("deployment_devops", "merge_pass1")
graph.add_edge("qa_testing_strategist", "merge_pass1")

# Human-in-the-loop: Stage 4 clarifying questions
graph.add_edge("merge_pass1", "await_user_answers")

# Pass 2: fan-out again (same topology, enriched context)
graph.add_edge("await_user_answers", "discovery_analyst")
graph.add_edge("await_user_answers", "ux_design_strategist")
graph.add_edge("await_user_answers", "development_architect")
graph.add_edge("await_user_answers", "code_review_sentinel")
graph.add_edge("await_user_answers", "deployment_devops")
graph.add_edge("await_user_answers", "qa_testing_strategist")

# Fan-in: Pass 2 convergence
graph.add_edge("discovery_analyst", "merge_pass2")
graph.add_edge("ux_design_strategist", "merge_pass2")
graph.add_edge("development_architect", "merge_pass2")
graph.add_edge("code_review_sentinel", "merge_pass2")
graph.add_edge("deployment_devops", "merge_pass2")
graph.add_edge("qa_testing_strategist", "merge_pass2")

# Post-processing pipeline (sequential)
graph.add_edge("merge_pass2", "consistency_check")
graph.add_edge("consistency_check", "commercial_processing")
graph.add_edge("commercial_processing", "synthesize_estimate")
graph.add_edge("synthesize_estimate", END)
```

**Note on twin node reuse:** Each twin node function detects whether it's running in Pass 1 or Pass 2 by checking whether `user_answers` is populated in the state. In Pass 1, twins produce preliminary estimates + gap lists. In Pass 2, twins produce final estimates and emit/receive A2A cross-phase signals.

#### 1.4.3 A2A Integration Within LangGraph Nodes

Each twin node wraps an A2A client/server internally. During Pass 2 execution:

1. **As A2A Server:** The twin listens for incoming cross-phase signals from upstream twins via SSE. When a signal arrives, the twin incorporates it into its reasoning before finalizing its estimate.
2. **As A2A Client:** After completing its core estimation logic but before returning its result, the twin emits cross-phase signals to downstream subscribers by sending A2A `message/send` requests.
3. **LangGraph coordination:** Even though A2A signals flow peer-to-peer, the LangGraph superstep still gates the fan-in — `merge_pass2` waits for all six twins to return, regardless of how many A2A signals have been exchanged between them.

This means twins run in parallel at the LangGraph level but can react to faster upstream twins via A2A before they finalize — achieving the "parallel but reactive" execution model.

#### 1.4.4 Human-in-the-Loop with LangGraph Interrupts

The `await_user_answers` node uses LangGraph's interrupt mechanism to pause graph execution while the user answers clarifying questions (Stage 4). The graph state is persisted (via LangGraph's built-in checkpointing), and execution resumes when the user submits their answers. This is critical for the UX — the user can take as long as they need without holding server resources.

### 1.5 LLM Infrastructure

- Model selection: which LLM(s) power the twins and the Orchestrator — same model for all, or different models optimized for different reasoning tasks. Consider using a more capable model (e.g., Claude Opus / GPT-4o) for the Orchestrator and Development Architect (highest complexity reasoning), and a faster model (e.g., Claude Sonnet / GPT-4o-mini) for twins with more constrained evaluation dimensions
- Prompt engineering: each twin needs a carefully designed system prompt that encodes its persona, evaluation dimensions, inference rules, output format, and A2A signal schemas (what signals it emits, what signals it listens for, and how to incorporate them)
- Context window management: the full context package (raw input + user answers + cross-phase signals) must fit within model limits. A2A's structured DataPart format helps keep payloads compact versus free-text context dumping
- Token cost estimation: seven agents per estimate (six twins + orchestrator), running twice (two-pass), plus cross-phase A2A signal exchanges in Pass 2 — estimate 14-20 LLM calls per estimate depending on how many cross-phase signals are exchanged. Model the cost per estimate at each pricing tier
- Rate limiting and concurrency: LangGraph's superstep model means six concurrent LLM calls per pass — ensure the LLM API rate limits support this. If multiple team members use the tool simultaneously, each estimation run requires its own LangGraph execution thread with isolated state
- Latency budget: Pass 1 target: 10-20 seconds (six parallel twins). Pass 2 target: 15-30 seconds (six parallel twins + A2A signal exchange adds latency for downstream twins waiting on upstream signals). Total LLM processing: 25-50 seconds. Full workflow including user interaction: under 10 minutes

---

## 2. Input Design

### Research Foundation

The input design draws from established estimation frameworks (COCOMO II's 17 cost drivers and 5 scale factors, Function Point Analysis's transaction/data decomposition, SEER-SEM's 50+ input parameters), modern AI estimation tools (devtimate, Cost Xpert, SLIM), and practitioner insights on what experienced estimators say matters most. The Cone of Uncertainty research confirms that estimates at initial concept can be off by 4x in either direction — the input design aims to narrow that cone as quickly as possible by capturing the highest-impact signals early.

### 2.1 Input Architecture: Progressive Disclosure with Four Stages

The input flow uses a staged progressive disclosure pattern — a wizard-style sequence where each stage builds on the previous one. This approach reduces cognitive load (users aren't overwhelmed with 30+ fields up front), accommodates the common case where users have limited structured information (they're working from messy sales notes), and allows the AI to do meaningful work between stages (extracting structure from unstructured input before asking for refinements).

**Stage 1: Raw Input Capture** → User provides what they have
**Stage 2: Project Context & Parameters** → User confirms/corrects AI-extracted signals + adds structured metadata
**Stage 3: AI Maturity & Team Assessment** → User rates capability levels across key dimensions
**Stage 4: AI-Generated Clarifying Questions** → User answers targeted questions based on gaps the twins identified

Each stage has a "skip and let AI infer" option — the tool should produce a usable estimate even if the user only completes Stage 1 and skips everything else. Skipped stages trigger inference with stated assumptions that appear in the final output.

---

### 2.2 Stage 1 — Raw Input Capture

**Purpose:** Accept whatever the user has — no formatting required, no mandatory fields. This is the only required stage.

#### 2.2.1 Primary Input: Unstructured Text Area

- Large text area (minimum 8 lines visible, expandable) for pasting or typing project descriptions
- Accepts: sales call notes, RFP excerpts, client meeting summaries, Slack thread pastes, email forwards, bullet-point feature lists, verbal transcriptions
- No character limit displayed (internally cap at ~50,000 characters for context window management, show a gentle warning above 30,000)
- Placeholder text shows a realistic example: a messy sales call note with incomplete sentences, abbreviations, and mixed topics — so users understand that polished prose is not expected
- Auto-save as the user types (debounced at 2 seconds) — losing a long paste is unacceptable

#### 2.2.2 File Upload (Optional)

- Drag-and-drop zone adjacent to (or below) the text area
- Accepted formats: PDF, DOCX, TXT, MD, PNG/JPG (for screenshots of specs or whiteboard photos), email (.eml/.msg)
- Multiple files allowed — the Orchestrator concatenates extracted text into the context package
- Show file name, size, and extraction status (parsing / ready / error) for each upload
- For image files: run OCR and display extracted text for user confirmation before including in context
- Maximum combined upload size: 25 MB (configurable)

#### 2.2.3 Project Name (Optional)

- Single text field, auto-generated from the first line of raw input if left blank
- Used for identification in the estimate history, not for estimation logic

#### 2.2.4 UI Layout — Stage 1

```
┌─────────────────────────────────────────────────────────────────────┐
│  NEW ESTIMATE                                                       │
│                                                                     │
│  Project Name (optional)                                            │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │ e.g., "Acme Corp Portal Rebuild"                              │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  Describe the project                                               │
│  Paste sales notes, RFP text, meeting summaries — whatever you     │
│  have. Raw and messy is fine.                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │                                                               │  │
│  │  "Call w/ Acme Corp - they want to rebuild their customer     │  │
│  │   portal. Currently running on a legacy JSP stack, ~15        │  │
│  │   years old. About 200 internal users, maybe 5000 external.   │  │
│  │   They mentioned needing SSO integration with their Okta      │  │
│  │   setup. Regulated industry (financial services). Timeline    │  │
│  │   pressure — want to launch by Q3. Team has some React        │  │
│  │   experience but no AI tooling yet..."                        │  │
│  │                                                               │  │
│  │                                                               │  │
│  │                                                               │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┐  │
│  │  📎 Drop files here or click to upload                        │  │
│  │  PDF, DOCX, TXT, images — up to 25 MB total                  │  │
│  └ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ┘  │
│                                                                     │
│            [ Generate Estimate ]        [ Skip to Quick Estimate ]  │
│                                                                     │
│  "Generate Estimate" → proceeds to Stage 2                         │
│  "Skip to Quick Estimate" → skips Stages 2-3, goes straight to    │
│  processing with full AI inference, then presents Stage 4           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

### 2.3 Stage 2 — Project Context & Parameters

**Purpose:** After the AI parses the raw input, it extracts structured signals and presents them for confirmation. The user corrects wrong extractions and fills in any high-value fields the AI couldn't infer. This stage also captures inputs inspired by COCOMO II cost drivers, SEER-SEM parameters, and practitioner-identified critical factors that the current plan was missing.

**Transition:** Between Stage 1 and Stage 2, the Orchestrator runs a parsing pass on the raw input. A brief loading state (2-5 seconds) shows "Analyzing your input..." with a progress indicator. The parsed results pre-populate Stage 2 fields. Fields the AI extracted with confidence are shown pre-filled with a subtle "AI-extracted" badge. Fields the AI couldn't determine are left blank.

#### 2.3.1 Client & Industry Profile

| Field | Type | Options / Format | Source |
|---|---|---|---|
| Client Industry | Dropdown with search | Financial Services, Healthcare, Government, Retail/E-commerce, Manufacturing, Education, Media/Entertainment, SaaS/Technology, Non-profit, Other (free text) | COCOMO II PLEX driver, practitioner insight |
| Regulatory Environment | Multi-select chips | SOC 2, HIPAA, PCI-DSS, GDPR, FedRAMP, SOX, FDA, None, Other | COCOMO II RELY driver, practitioner insight |
| Client Size | Segmented control | Startup (<50), SMB (50-500), Mid-market (500-5000), Enterprise (5000+) | SEER-SEM org complexity factor |
| Client Technical Sophistication | Slider (1-5) with labels | 1: Non-technical org, 3: Has internal dev team, 5: Engineering-first org | Practitioner insight — client capability affects communication overhead |

#### 2.3.2 Project Scope & Sizing (Research Gap: Function Point / Use Case Sizing)

These fields capture sizing signals that COCOMO II, Function Point Analysis, and SEER-SEM all identify as primary estimation drivers. The current plan had no explicit sizing mechanism.

| Field | Type | Options / Format | Source |
|---|---|---|---|
| Project Type | Dropdown | New Build (Greenfield), Legacy Replacement/Modernization, Enhancement/Feature Add, Integration/Middleware, Data Migration, AI/ML System Build | SEER-SEM project type, practitioner insight |
| Estimated User-Facing Screens/Views | Number input with range | Numeric, or range selector: <10, 10-25, 25-50, 50-100, 100+ | Function Point Analysis proxy (EO/EQ count) |
| Distinct User Roles | Number input | Numeric (1-20+) | FPA transaction complexity driver |
| Core Workflows / Business Processes | Number input with range | Numeric, or range: <5, 5-15, 15-30, 30+ | FPA External Input proxy |
| External System Integrations | Number input + list builder | Count + names if known (e.g., "Okta SSO," "Salesforce API," "Legacy DB") | COCOMO II DATA driver, SEER-SEM integration complexity |
| Data Migration Required? | Toggle + complexity selector | Yes/No; if Yes: Simple (structured, clean), Moderate (multiple sources, some cleanup), Complex (unstructured, legacy formats, transformation logic) | Practitioner insight — consistently underestimated |
| Estimated Data Volume | Segmented control | Small (<1GB), Medium (1-100GB), Large (100GB-1TB), Very Large (1TB+) | COCOMO II DATA driver |

#### 2.3.3 Engagement & Commercial Parameters (Research Gap: Contract/Pricing Context)

These inputs were entirely missing from the current plan. Research on fixed-price vs. T&M contracts shows that the contract model fundamentally changes estimation strategy — fixed-price needs contingency buffers (15-30% risk premiums), while T&M needs accurate burn-rate projections. Experienced estimators consistently cite this as a top-5 input.

| Field | Type | Options / Format | Source |
|---|---|---|---|
| Engagement Model | Segmented control | Fixed Price, Time & Materials, Retainer/Staff Aug, Hybrid, Not Yet Determined | Contract type research, practitioner insight |
| Target Timeline | Date range or duration | Calendar picker for start/end, or duration selector: <1 month, 1-3 months, 3-6 months, 6-12 months, 12+ months | COCOMO II SCED (schedule) driver |
| Timeline Flexibility | Slider (1-5) | 1: Hard deadline, immovable / 3: Preferred but negotiable / 5: Flexible, quality over speed | COCOMO II SCED driver |
| Budget Range Known? | Toggle + range input | Yes/No; if Yes: $ low - $ high | Practitioner insight — helps calibrate scope expectations |
| Include PM Overhead? | Toggle + percentage | Yes/No; if Yes: percentage (default 15-20%) or let AI calculate based on project complexity | Practitioner insight |

**Role-Based Rate Table**

Instead of a single blended hourly rate, the system uses a four-role staffing model that reflects how work is actually priced and delivered. The rate table is configured at the organization level (Settings) and can be overridden per estimate:

| Role | Description | Default Rate (configurable) |
|---|---|---|
| **Sr. Product Engineer** | Leads feature design, UX implementation, user-facing workflows, component architecture. Drives discovery sessions, translates business requirements into technical specs, owns product-facing code quality. | $— /hr (org setting) |
| **Jr. Product Engineer** | Implements UI components, builds screens from designs, writes user-facing tests, handles data binding and form logic. Works under Sr. Product Engineer guidance. | $— /hr (org setting) |
| **Sr. Technical Engineer** | Leads architecture decisions, infrastructure design, system integration, performance optimization, security implementation. Owns backend systems, data models, CI/CD, and DevOps. Handles legacy modernization and complex technical debt. | $— /hr (org setting) |
| **Jr. Technical Engineer** | Implements backend endpoints, writes unit/integration tests, builds data access layers, configures infrastructure from established patterns. Works under Sr. Technical Engineer guidance. | $— /hr (org setting) |

**Why this split matters for estimation:**
- **Product vs. Technical** maps to how effort distributes across front-of-stack (user-facing) vs. back-of-stack (systems/infrastructure) work. A consumer-facing app may be 60% product / 40% technical; a data pipeline project may be 10% product / 90% technical. Each twin's output attributes hours to the appropriate role type.
- **Junior vs. Senior** reflects both cost and velocity. Senior engineers are more expensive per hour but complete complex work faster and with fewer revisions. Junior engineers are cheaper but need more supervision hours (which come from seniors). The ratio affects both the total cost and the calendar duration. A team heavy on juniors is cheaper per hour but takes longer and generates more Code Review overhead.
- The Orchestrator uses this rate table in the `commercial_processing` node to convert role-attributed hours into cost ranges, replacing the old single blended rate calculation.

The rate table UI in the input form:

```
┌─── Rate Table ──────────────────────────────────────────────────┐
│  Using org defaults · [ Override for this estimate ]            │
│                                                                  │
│  Role                        Rate/hr                             │
│  Sr. Product Engineer        $___.__                             │
│  Jr. Product Engineer        $___.__                             │
│  Sr. Technical Engineer      $___.__                             │
│  Jr. Technical Engineer      $___.__                             │
│                                                                  │
│  [ Reset to org defaults ]                                       │
└──────────────────────────────────────────────────────────────────┘
```

#### 2.3.4 Technical Environment (Research Gap: COCOMO II Platform & Environment Drivers)

These capture the hardware/platform constraints and development environment factors that COCOMO II's platform-related cost drivers address and that SEER-SEM considers material to estimation accuracy.

| Field | Type | Options / Format | Source |
|---|---|---|---|
| Target Platform(s) | Multi-select chips | Web App, Mobile (iOS), Mobile (Android), Desktop, API/Backend Only, Embedded/IoT | SEER-SEM platform factor |
| Known Tech Stack | Tag input (free text) | e.g., React, Node.js, PostgreSQL, AWS, Kubernetes — auto-suggest from common stacks | COCOMO II PLEX (platform experience) |
| Existing Codebase? | Toggle + detail | Yes/No; if Yes: Approx LOC, Language(s), Age (years), Quality assessment (Clean / Manageable / Legacy debt) | COCOMO II RUSE, practitioner insight |
| Hosting/Infrastructure | Dropdown | Client's existing cloud (specify), New cloud setup needed, On-premise, Hybrid, Unknown | SEER-SEM environment factor |
| Security Requirements | Multi-select chips + severity | Authentication (SSO/MFA), Authorization (RBAC/ABAC), Encryption (at-rest/in-transit), Penetration testing, Security audit, Compliance certification | COCOMO II RELY, NFR research |
| Performance Requirements | Segmented control | Standard (typical web app), High (sub-second response, high concurrency), Critical (real-time, financial trading, healthcare monitoring) | COCOMO II TIME/STOR drivers, NFR research |

#### 2.3.5 Non-Functional Requirements (Research Gap: Commonly Missed NFRs)

Research consistently shows non-functional requirements are the most underestimated category in software projects. These fields make NFR impact explicit in the estimate rather than leaving it to implicit assumptions.

| Field | Type | Options / Format | Source |
|---|---|---|---|
| Accessibility Requirements | Dropdown | None specified, WCAG 2.1 AA, WCAG 2.1 AAA, Section 508, Custom | NFR research, practitioner insight |
| Internationalization / Localization | Toggle + detail | Yes/No; if Yes: Number of languages, RTL support needed? | NFR research |
| Uptime / SLA Requirements | Dropdown | Best effort, 99.5%, 99.9%, 99.99% | NFR research, COCOMO II RELY |
| Documentation Deliverables | Multi-select chips | User documentation, API documentation, Admin/ops runbooks, Architecture decision records, Training materials | Practitioner insight — often added late, always underestimated |
| Post-Launch Support Scope | Dropdown | None (handoff at launch), Warranty period (specify weeks), Ongoing maintenance contract | Practitioner insight |

#### 2.3.6 UI Layout — Stage 2

```
┌─────────────────────────────────────────────────────────────────────┐
│  PROJECT CONTEXT                                         Step 2/4   │
│                                                                     │
│  We extracted the following from your input. Confirm or correct     │
│  what we got, and fill in anything we missed. All fields are        │
│  optional — we'll infer what you skip.                             │
│                                                                     │
│  ┌─── Client & Industry ──────────────────────────────────────┐    │
│  │  Industry: [Financial Services ▾] ✦ AI-extracted            │    │
│  │  Regulations: [SOC 2] [PCI-DSS] ✦ AI-extracted              │    │
│  │  Client Size: ( ) Startup (●) SMB ( ) Mid-market ( ) Ent.  │    │
│  │  Technical Sophistication: ───●─────── 3/5                  │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌─── Project Scope & Sizing ─────────────────────────────────┐    │
│  │  Project Type: [Legacy Replacement ▾] ✦ AI-extracted        │    │
│  │  Screens/Views: [~25] ✦ AI-estimated                        │    │
│  │  User Roles: [3]  Workflows: [~10]                          │    │
│  │  Integrations: [Okta SSO ×] [Legacy DB ×] [+ Add]          │    │
│  │  Data Migration: (●) Yes  Complexity: [Moderate ▾]          │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ▸ Engagement & Commercial Parameters          (expand section)    │
│  ▸ Technical Environment                       (expand section)    │
│  ▸ Non-Functional Requirements                 (expand section)    │
│                                                                     │
│    [ ← Back ]    [ Next: AI Maturity → ]    [ Skip remaining → ]   │
│                                                                     │
│  Confidence: ████████░░ 78% — 4 fields AI-extracted, 12 blank     │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Key UI behaviors:**
- Sections below the first two are collapsed by default (progressive disclosure) — expand on click
- "AI-extracted" badges (✦) indicate fields the parser pre-filled; user can edit any of them
- Confidence meter at the bottom updates live as fields are filled — shows the user how their input narrows the Cone of Uncertainty
- "Skip remaining →" advances to Stage 3 (or Stage 4 if Stage 3 is also skipped)

---

### 2.4 Stage 3 — AI Maturity & Team Assessment

**Purpose:** Capture the client's current AI adoption level and the delivery team's characteristics. This is the critical differentiator for AI SDLC estimation — it determines what percentage of each phase can be AI-assisted versus requiring traditional human effort. Research from COCOMO II's personnel-related cost drivers (ACAP, PCAP, AEXP, PEXP, LTEX) and practitioner insights on team factors confirm that team capability is among the highest-impact estimation inputs.

#### 2.4.1 AI Maturity Assessment (Per Phase)

Each phase gets an independent maturity rating. The UI uses a visual slider or segmented control with descriptive labels at each level, so users don't need to understand abstract maturity scales.

| Phase | Level 1 (No AI) | Level 2 (Exploring) | Level 3 (Adopting) | Level 4 (Integrated) | Level 5 (Advanced/Agentic) |
|---|---|---|---|---|---|
| Discovery | Manual requirements gathering only | Using AI for meeting transcription | AI-assisted gap analysis on requirements | AI generates draft requirements from stakeholder input | Fully AI-driven requirements with human validation |
| UX/Design | Traditional design process | AI for mood boards / inspiration | AI-generated wireframes from specs | AI produces full design systems from requirements | AI generates production-ready designs with minimal human adjustment |
| Development | No AI coding tools | GitHub Copilot or similar autocomplete | AI generates functions/modules with human review | AI builds features end-to-end with architectural guidance | Agentic AI handles full feature development autonomously |
| Code Review | Purely human review | Linting + static analysis only | AI-assisted review suggestions | AI performs first-pass review, humans handle edge cases | AI review with intelligence loop and pattern learning |
| Deployment | Manual deployment process | Basic CI/CD pipelines | AI-optimized deployment with quality gates | AI manages rollout strategy and monitoring | Autonomous deployment with AI-driven rollback decisions |
| QA/Testing | Manual test writing and execution | AI-assisted test case generation | AI generates and maintains test suites | AI performs exploratory testing with human oversight | AI handles full QA lifecycle including edge case discovery |

**Default behavior:** If the user skips this section entirely, all phases default to Level 1 (most conservative estimate). The Orchestrator states this assumption in the output.

#### 2.4.1B AI Effectiveness by Role (Per Phase)

Each phase also accepts an optional **AI effectiveness override per role**. This captures the reality that AI tools impact senior and junior resources very differently — a senior engineer may leverage AI to accelerate architectural scaffolding and complex logic, while a junior engineer may use the same tools primarily for boilerplate generation but require more time validating AI output. Conversely, in some phases (e.g., QA), juniors may benefit more from AI test generation filling knowledge gaps.

The default values are derived from research and the AI maturity level set above. Users can override per cell if their team's experience differs from the defaults.

| Phase | Sr. Product Eng. | Jr. Product Eng. | Sr. Technical Eng. | Jr. Technical Eng. |
|---|---|---|---|---|
| Discovery | High (seniors lead stakeholder sessions; AI augments analysis) | Low (juniors assist but AI adds less to note-taking tasks) | Medium (seniors architect discovery frameworks) | Low (juniors do data gathering; AI minimally helpful) |
| UX/Design | High (seniors direct AI design tools effectively) | Medium (juniors can use AI for wireframe generation) | Low (technical engineers rarely involved in design) | Low |
| Development | Very High (seniors prompt AI for architecture + complex logic) | High (juniors use AI for boilerplate + standard patterns) | Very High (seniors leverage AI across full stack) | Medium (juniors need more time validating AI output) |
| Code Review | High (seniors use AI-assisted review to handle volume) | Low (juniors lack context to validate AI review suggestions) | High (seniors use AI for static analysis triage) | Low (juniors still learning review patterns) |
| Deployment | Medium (seniors configure AI-optimized pipelines) | Low | High (seniors automate IaC with AI assistance) | Medium (juniors handle templated tasks well with AI) |
| QA/Testing | Medium (seniors design AI test strategies) | High (juniors leverage AI for test case generation — fills knowledge gaps) | Medium (seniors validate AI-generated test architectures) | High (juniors generate tests effectively with AI) |

**AI Effectiveness Rating Scale:**

| Rating | AI Effort Reduction (applied to that role's hours in that phase) |
|---|---|
| None | 0% |
| Low | 5–10% |
| Medium | 15–25% |
| High | 25–40% |
| Very High | 40–55% |

These per-role AI effectiveness values feed into each twin's estimation algorithm. Every twin produces **two sets of hour estimates**: (1) **AI-assisted** — applying the role-specific AI reductions, and (2) **Manual-only** — with all AI reductions set to 0%. This dual output makes the AI value proposition explicit: stakeholders can see exactly how many hours (and dollars) AI tooling saves per phase, per role.

**Interaction with AI Maturity Level:** The AI maturity level (Section 2.4.1) sets the ceiling for AI effectiveness. A role rated "Very High" effectiveness at AI Maturity Level 1 (No AI) still gets 0% reduction — the tools aren't available. The twin calculates: `Effective_AI_Reduction = min(role_AI_effectiveness, maturity_ceiling)`. Maturity ceilings: Level 1 = 0%, Level 2 = 10%, Level 3 = 30%, Level 4 = 45%, Level 5 = 55%.

**Why seniors can skew estimates significantly:** At senior billing rates ($200–300+/hr), even a 10% difference in AI effectiveness translates to large dollar impacts. A 30% AI reduction on 500 senior hours at $250/hr = $37,500 savings. The same 30% on 500 junior hours at $120/hr = $18,000. The dual-output model makes this visible in the estimate, enabling informed staffing decisions.

#### 2.4.2 Delivery Team Profile (Research Gap: COCOMO II Personnel Drivers)

COCOMO II identifies five personnel-related cost drivers as among the most impactful on estimation accuracy. The team profile captures role effort distribution (what percentage of work each role handles) and capability factors. Team headcount (how many people in each role) is NOT an input — it is an output recommended by the twins based on their effort estimates and the target timeline (see Section 5.1).

**Role Effort Distribution (Percentages)**

The user specifies what percentage of total effort each of the four roles should handle. This is a project-level input that the Orchestrator distributes to all twins — each twin then applies these percentages to its own phase effort, adjusted for the nature of the work in that phase (see Section 4.5.1 for how twins interpret project-level percentages).

| Field | Type | Options / Format | Default | Source |
|---|---|---|---|---|
| Sr. Product Engineer % | Slider or number input | 0-100% | 25% | Role-based staffing model |
| Jr. Product Engineer % | Slider or number input | 0-100% | 25% | Role-based staffing model |
| Sr. Technical Engineer % | Slider or number input | 0-100% | 25% | Role-based staffing model |
| Jr. Technical Engineer % | Slider or number input | 0-100% | 25% | Role-based staffing model |

Must sum to 100%. The UI enforces this with a linked slider control — adjusting one percentage automatically redistributes the difference across the other three (proportionally), with a manual override toggle to set each independently.

**Quick presets** for common effort distributions (clicking a preset fills the four percentage fields, which the user can then adjust):

| Preset | Sr. Product | Jr. Product | Sr. Technical | Jr. Technical | Typical Use |
|---|---|---|---|---|---|
| Balanced | 25% | 25% | 25% | 25% | General-purpose default |
| Product-Heavy | 30% | 25% | 25% | 20% | Consumer-facing app, design-intensive |
| Technical-Heavy | 15% | 10% | 40% | 35% | Backend / data / infra / migration |
| Senior-Heavy | 35% | 10% | 40% | 15% | Complex architecture, tight timeline |
| Junior-Heavy | 15% | 30% | 15% | 40% | Budget-constrained, well-defined scope |
| Product-Only | 50% | 50% | 0% | 0% | Pure frontend / design engagement |
| Technical-Only | 0% | 0% | 50% | 50% | Pure infrastructure / backend engagement |

**Why percentages are an input but headcount is an output:**
- Percentages reflect a staffing strategy decision the user makes based on their delivery model, client relationship, and team availability. It answers "what kind of team do we want on this project?"
- Headcount is a derived quantity that depends on the effort estimate AND the target timeline — it answers "how many people do we need to deliver these hours in this timeframe?" The twins are better positioned to answer this because they know the effort breakdown, phase dependencies, and parallelism constraints
- This separation prevents circular reasoning: if headcount were an input, the twins would need to estimate effort for a fixed team, but the right team size depends on the effort. By taking percentages as input and deriving headcount as output, the estimation flows in one direction

**Team Capability Factors**

| Field | Type | Options / Format | Source |
|---|---|---|---|
| Team Domain Experience | Slider (1-5) | 1: No prior experience in this domain / 3: Some related projects / 5: Deep domain experts | COCOMO II AEXP (applications experience) |
| Team Platform Experience | Slider (1-5) | 1: New to this tech stack / 3: Moderate experience / 5: Platform experts | COCOMO II PEXP (platform experience) |
| Team Continuity Risk | Segmented control | Stable (dedicated team throughout), Some turnover expected, High turnover risk, Team not yet assembled | COCOMO II PCON (personnel continuity) |
| Development Process Maturity | Slider (1-5) | 1: Ad hoc / 2: Basic processes / 3: Defined methodology / 4: Measured and managed / 5: Optimizing (CMMI-aligned) | COCOMO II PMAT scale factor |

**Junior-to-Senior ratio effects on estimation (derived from the input percentages):**
- A high junior-to-senior ratio (Jr. Product + Jr. Technical > 2× Sr. Product + Sr. Technical) increases calendar duration (juniors are slower per task), increases Code Review twin's effort (more PRs need thorough review), and adds mentorship/supervision overhead to senior hours (typically 10-20% of senior time consumed by junior guidance)
- A low junior-to-senior ratio reduces duration and review overhead but increases cost (more hours billed at senior rates)
- The Orchestrator flags this tradeoff explicitly in the output: "Your role distribution is X% junior / Y% senior. Shifting 10% from junior to senior would increase cost by $Z but reduce duration by W weeks."

#### 2.4.3 UI Layout — Stage 3

```
┌─────────────────────────────────────────────────────────────────────┐
│  AI MATURITY & TEAM                                      Step 3/4   │
│                                                                     │
│  How does the client (or your team) currently use AI across the    │
│  development lifecycle? This directly affects effort estimates.     │
│                                                                     │
│  ┌─── AI Maturity by Phase ───────────────────────────────────┐    │
│  │                                                             │    │
│  │  Discovery                                                  │    │
│  │  [●] No AI  [ ] Exploring  [ ] Adopting  [ ] Integrated    │    │
│  │  "Manual requirements gathering only"                       │    │
│  │                                                             │    │
│  │  UX / Design                                                │    │
│  │  [ ] No AI  [●] Exploring  [ ] Adopting  [ ] Integrated    │    │
│  │  "Using AI for mood boards / inspiration"                   │    │
│  │                                                             │    │
│  │  Development                                                │    │
│  │  [ ] No AI  [ ] Exploring  [●] Adopting  [ ] Integrated    │    │
│  │  "AI generates functions/modules with human review"         │    │
│  │                                                             │    │
│  │  Code Review                                                │    │
│  │  [●] No AI  [ ] Exploring  [ ] Adopting  [ ] Integrated    │    │
│  │                                                             │    │
│  │  Deployment                                                 │    │
│  │  [ ] No AI  [●] Exploring  [ ] Adopting  [ ] Integrated    │    │
│  │                                                             │    │
│  │  QA / Testing                                               │    │
│  │  [●] No AI  [ ] Exploring  [ ] Adopting  [ ] Integrated    │    │
│  │                                                             │    │
│  │  [ Set all to same level ▾ ]                                │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ▸ Delivery Team Profile                       (expand section)    │
│    Role Effort %, Domain Experience, Platform Experience, etc.      │
│                                                                     │
│    [ ← Back ]    [ Next: Review Questions → ]  [ Skip → ]         │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Key UI behaviors:**
- Selecting a maturity level shows a brief description below the selector explaining what that level means in practice
- "Set all to same level" dropdown for users who want to quickly set a uniform baseline
- The "Advanced/Agentic" (Level 5) option is hidden by default and only shown via a "Show advanced options" toggle — most clients won't be there yet, and showing it by default creates anchoring bias
- Delivery Team Profile is collapsed by default since team may not be assembled yet for early-stage estimates

---

### 2.5 Stage 4 — AI-Generated Clarifying Questions

**Purpose:** After Pass 1 processing, the twins identify specific gaps in the input that would meaningfully change their estimates. The Orchestrator deduplicates, prioritizes by estimate impact, and presents 5-10 focused questions. This is not a static questionnaire — the questions are contextual to the specific project.

#### 2.5.1 Question Design Principles

- Every question includes a suggested default with reasoning: "Based on the client being in financial services, we're assuming 3-5 stakeholder groups requiring separate approval workflows. Is that right?"
- All questions are skippable — "Use suggested default" is always one click
- Questions are ordered by estimate impact: the ones that swing the total number most come first
- Each question shows which twin(s) asked it and which phase(s) it affects (as a subtle tag, not prominently — the user shouldn't need to understand the twin architecture)
- Questions should never repeat information already captured in Stages 1-3 — the twins receive the full context package and only ask about genuine gaps
- Maximum 10 questions displayed; if twins generate more, the Orchestrator prioritizes by composite impact score and discards the rest (using defaults for those gaps)

#### 2.5.2 Question Format Types

| Format | Use Case | Example |
|---|---|---|
| Confirm/Deny with default | Binary decisions where the AI has a strong inference | "We're assuming the client wants SSO integration with their existing Okta instance. Correct? [Yes, Okta SSO] [No, different approach] [Skip]" |
| Multiple choice | Discrete options that significantly affect scope | "What level of data migration validation does the client expect? [Spot-check samples] [Full reconciliation report] [Parallel-run validation period] [Skip — use default: spot-check]" |
| Numeric range | Quantitative inputs the AI couldn't extract | "Approximately how many concurrent users should the system support at peak? [<100] [100-1,000] [1,000-10,000] [10,000+] [Skip — use default: 100-1,000]" |
| Free text with suggestion | Open-ended context that might change an assumption | "Are there any third-party systems beyond Okta and the legacy DB that need integration? [Text field pre-filled with: 'None identified'] [Skip]" |

#### 2.5.3 Question Categorization (For Orchestrator Logic)

The Orchestrator scores and categorizes candidate questions from all six twins:

- **Scope-defining questions** — change what is built (highest impact, show first)
- **Complexity-calibrating questions** — change how hard it is to build (high impact)
- **Risk-surfacing questions** — identify unknowns that should appear as contingency (medium impact)
- **Preference questions** — affect approach but not effort significantly (lower impact, show last or drop)

#### 2.5.4 UI Layout — Stage 4

```
┌─────────────────────────────────────────────────────────────────────┐
│  CLARIFYING QUESTIONS                                    Step 4/4   │
│                                                                     │
│  Based on your input, we have a few questions that would improve   │
│  accuracy. Each has a suggested default — skip any you're unsure   │
│  about and we'll use the default.                                  │
│                                                                     │
│  ─────────────────────────────────────────────────────────────────  │
│                                                                     │
│  1 of 7 · Affects: Discovery, Development           SCOPE          │
│                                                                     │
│  How many distinct approval workflows does the client need?        │
│  The raw input mentions "multiple stakeholder groups" — this       │
│  significantly affects both discovery and development effort.      │
│                                                                     │
│  Suggested default: 3-5 workflows (based on regulated financial    │
│  services industry pattern)                                        │
│                                                                     │
│     [ 1-2 ]  [ 3-5 ]  [ 6-10 ]  [ 10+ ]  [ Use default ]         │
│                                                                     │
│  ─────────────────────────────────────────────────────────────────  │
│                                                                     │
│  2 of 7 · Affects: Development, Deployment          COMPLEXITY     │
│                                                                     │
│  The legacy JSP system — does it have a usable API layer, or      │
│  will integrations need to go through the database directly?       │
│                                                                     │
│  Suggested default: No API layer (15-year-old JSP systems          │
│  typically lack modern API infrastructure)                         │
│                                                                     │
│     [ Has REST/SOAP APIs ]                                         │
│     [ Database-level integration only ]                            │
│     [ Unknown — use default ]                                      │
│                                                                     │
│  ─────────────────────────────────────────────────────────────────  │
│                                                                     │
│  ...                                                                │
│                                                                     │
│       [ ← Back ]    [ Use defaults for remaining ]                 │
│                              [ Generate Estimate → ]               │
│                                                                     │
│  Progress: ████████████████░░░░ 4/7 answered                      │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

**Key UI behaviors:**
- One question at a time (card-style) or scrollable list — configurable per user preference
- "Use defaults for remaining" button always visible — lets users exit early when they've answered the high-impact questions
- Progress bar shows answered/total
- After the last question (or "Use defaults for remaining"), the "Generate Estimate" button triggers Pass 2 processing
- Brief loading state during Pass 2: "Generating your estimate..." with per-twin progress indicators (optional: show which twin is currently processing)

---

### 2.6 Stage 5 — Estimate Review & Adjustment

**Purpose:** After Pass 2 completes, present the full estimate for review. The user can adjust any numbers, and the system flags downstream impacts of manual changes. This stage transforms the estimate from a model output into a proposal-ready deliverable.

#### 2.6.1 Summary View (Default Landing)

- Total effort range: Low / Mid / High (hours)
- Total cost range: Low / Mid / High (hours × blended rate, if rate was provided)
- Total duration estimate: based on team size and effort, accounting for parallelism
- Per-phase breakdown as a horizontal stacked bar chart (visual) + table (detail)
- Overall confidence score based on: percentage of fields filled, number of clarifying questions answered, historical calibration data availability for this project profile
- Cone of Uncertainty indicator: "Based on the information provided, this estimate is likely accurate within ±X%"
- Key assumptions summary: top 5 assumptions that most affect the number, each clickable to expand reasoning
- Risk register summary: top 3 risks with potential effort impact

#### 2.6.2 Detail View (Expandable Per Phase)

- Click any phase in the summary to expand its full breakdown
- Per-phase: effort range (low/mid/high), assumptions list with confidence levels, risk flags with impact ranges, dependency notes
- Each number has an "edit" affordance — clicking opens an inline editor where the user can override the value
- When a manual override is made: the original AI estimate is shown struck-through, the override value replaces it, and a yellow warning banner appears listing downstream phases that may be affected with suggested adjustments
- Track which values are AI-generated vs. manually adjusted (displayed as subtle badges)

#### 2.6.3 Adjustment Workflow

- Manual override → system recalculates downstream impacts → user reviews impact warnings → accepts or reverts
- "Re-estimate this phase" button re-runs a single twin with updated context (useful after significant manual changes to upstream phases)
- "Lock this phase" toggle prevents a phase estimate from changing during re-estimation — useful when a specialist has already validated a specific phase

#### 2.6.4 Export & Actions

- "Copy to clipboard" — formatted markdown suitable for pasting into a proposal
- "Export to spreadsheet" — detailed breakdown in XLSX format
- "Save estimate" — stores in the system for version history and future calibration
- "Start new estimate from this" — clone and modify for variant scenarios (e.g., "what if we cut the data migration scope?")
- "Compare estimates" — if previous estimates exist, side-by-side comparison view

#### 2.6.5 UI Layout — Estimate Review

```
┌─────────────────────────────────────────────────────────────────────┐
│  ESTIMATE: Acme Corp Portal Rebuild                                │
│  Generated May 27, 2026 · Confidence: 74%                         │
│  Cone of Uncertainty: ±30% (typical for initial concept stage)     │
│                                                                     │
│  ┌─── Summary ────────────────────────────────────────────────┐    │
│  │                                                             │    │
│  │  Total Effort       640 — 920 — 1,340 hours                │    │
│  │  Total Cost         $110K — $159K — $232K                  │    │
│  │  Est. Duration      12 — 16 — 22 weeks                     │    │
│  │  Burn Rate          ~$10K/wk (mid estimate)                │    │
│  │                                                             │    │
│  │  View: [By Phase ●] [By Role ○]                            │    │
│  │                                                             │    │
│  │  ── By Phase ──────────────────────────────────────────     │    │
│  │  ┌───────────────────────────────────────────────────┐      │    │
│  │  │▓▓▓▓░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░│      │    │
│  │  │Disc│  UX  │    Development    │ CR │Deploy│  QA  │      │    │
│  │  │ 8% │ 12%  │       35%        │10% │ 15%  │ 20%  │      │    │
│  │  └───────────────────────────────────────────────────┘      │    │
│  │                                                             │    │
│  │  ── By Role (mid estimate) ────────────────────────────     │    │
│  │  Sr. Product Eng.    198 hrs   $___K   ████████░░  22%    │    │
│  │  Jr. Product Eng.    184 hrs   $___K   ███████░░░  20%    │    │
│  │  Sr. Technical Eng.  322 hrs   $___K   ████████████ 35%   │    │
│  │  Jr. Technical Eng.  216 hrs   $___K   █████████░░  23%   │    │
│  │                                                             │    │
│  │  Key Assumptions (5)        Risk Flags (3)                 │    │
│  │  ▸ No API layer on legacy   ▸ Data migration complexity    │    │
│  │  ▸ 3-5 approval workflows   ▸ Timeline pressure vs scope  │    │
│  │  ▸ Team AI maturity: low    ▸ Legacy system documentation  │    │
│  │  ...                                                        │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌─── Phase Detail ───────────────────────────────────────────┐    │
│  │  ▸ Discovery Analyst           52 — 74 — 107 hrs          │    │
│  │  ▸ UX/Design Strategist        77 — 110 — 161 hrs         │    │
│  │  ▾ Development Architect       224 — 322 — 469 hrs    ✎   │    │
│  │    ├ Setup & Configuration      24 — 32 — 48 hrs          │    │
│  │    ├ Feature Build             160 — 240 — 360 hrs         │    │
│  │    ├ Integration Work           40 — 50 — 61 hrs          │    │
│  │    ├ Role Breakdown (mid):                                 │    │
│  │    │   Sr. Product:  48 hrs  (15%)                         │    │
│  │    │   Jr. Product:  97 hrs  (30%)                         │    │
│  │    │   Sr. Technical: 97 hrs (30%)                         │    │
│  │    │   Jr. Technical: 80 hrs (25%)                         │    │
│  │    └ Assumptions:                                          │    │
│  │      • AI-to-human ratio: 30% AI / 70% human (low mat.)  │    │
│  │      • 12-15 features estimated from scope description    │    │
│  │      • Legacy DB integration adds ~40% to integration hrs │    │
│  │  ▸ Code Review Sentinel         64 — 92 — 134 hrs         │    │
│  │  ▸ Deployment & DevOps         96 — 138 — 201 hrs         │    │
│  │  ▸ QA & Testing               128 — 184 — 268 hrs         │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  ┌─── Recommended Staffing ────────────────────────────────────┐    │
│  │  For a 16-week timeline (mid estimate):                     │    │
│  │                                                             │    │
│  │  Role              Hrs    People  Util.   $/hr   Cost      │    │
│  │  Sr. Product Eng.   198    1       77%    $__    $___K     │    │
│  │  Jr. Product Eng.   184    2       36%*   $__    $___K     │    │
│  │  Sr. Technical Eng. 322    2       63%    $__    $___K     │    │
│  │  Jr. Technical Eng. 216    2       42%*   $__    $___K     │    │
│  │                                                             │    │
│  │  * Jr. roles show lower utilization because their work     │    │
│  │    concentrates in Development + QA phases, not all 16 wks │    │
│  │                                                             │    │
│  │  Your role split: 22% / 20% / 35% / 23%                   │    │
│  │  Requested split:  25% / 25% / 25% / 25%                  │    │
│  │  ▸ Discovery deviated: 65/10/20/5 (senior-weighted)        │    │
│  │  ▸ Deployment deviated: 5/5/60/30 (technical-weighted)     │    │
│  │                                                             │    │
│  │  [ What-if: shift Jr→Sr % ] [ What-if: change timeline ]  │    │
│  └─────────────────────────────────────────────────────────────┘    │
│                                                                     │
│  [ Copy to Clipboard ] [ Export XLSX ] [ Save ] [ Compare ]        │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

### 2.7 Complete Input Field Inventory

For reference, here is the complete list of all input fields across all stages, with their data types, default behavior when skipped, and which twin(s) consume each field:

#### Stage 1 (Required)
| # | Field | Type | Default if Skipped | Consumed By |
|---|---|---|---|---|
| 1 | Raw project description | Free text | N/A (required) | All twins via Orchestrator |
| 2 | File uploads | File(s) | No files | All twins (parsed text appended to context) |
| 3 | Project name | Text | Auto-generated | Display only |

#### Stage 2 (All Optional — AI-Inferred or Defaulted)
| # | Field | Default if Skipped | Consumed By |
|---|---|---|---|
| 4 | Client Industry | AI-inferred from text, else "General/Unknown" | Discovery, all twins (regulatory inference) |
| 5 | Regulatory Environment | AI-inferred, else "None" | Discovery, Deployment, QA |
| 6 | Client Size | "SMB" default | Discovery (stakeholder complexity), UX (user count proxy) |
| 7 | Client Technical Sophistication | 3/5 (moderate) | All twins (communication overhead factor) |
| 8 | Project Type | AI-inferred, else "New Build" | All twins (baseline effort profiles) |
| 9 | Estimated Screens/Views | AI-estimated from scope description | UX, Development, QA |
| 10 | Distinct User Roles | AI-estimated, else 3 | UX, QA |
| 11 | Core Workflows | AI-estimated, else 5-10 | Discovery, UX, Development, QA |
| 12 | External Integrations | AI-extracted from text, else 0 | Development, Deployment |
| 13 | Data Migration | "No" default | Development, QA |
| 14 | Data Volume | "Medium" default | Development, Deployment |
| 15 | Engagement Model | "Not Yet Determined" | Orchestrator (contingency calculation) |
| 16 | Sr. Product Engineer Rate | Org default | Orchestrator (role-based cost calculation) |
| 17 | Jr. Product Engineer Rate | Org default | Orchestrator (role-based cost calculation) |
| 18 | Sr. Technical Engineer Rate | Org default | Orchestrator (role-based cost calculation) |
| 19 | Jr. Technical Engineer Rate | Org default | Orchestrator (role-based cost calculation) |
| 20 | Target Timeline | AI-inferred, else "Not specified" | All twins (schedule pressure) |
| 21 | Timeline Flexibility | 3/5 (moderate) | All twins (SCED factor) |
| 22 | Budget Range | "Not specified" | Orchestrator (scope calibration) |
| 23 | PM Overhead | "Yes, 15%" default | Orchestrator (composite calculation) |
| 24 | Target Platforms | AI-inferred, else "Web App" | Development, QA, Deployment |
| 25 | Known Tech Stack | AI-extracted from text, else blank | Development, Code Review |
| 26 | Existing Codebase | "No" default | Development, Code Review |
| 27 | Hosting/Infrastructure | "Unknown" default | Deployment |
| 28 | Security Requirements | AI-inferred from industry, else "Standard" | Development, Deployment, QA |
| 29 | Performance Requirements | "Standard" default | Development, Deployment, QA |
| 30 | Accessibility | "None specified" default | UX, Development, QA |
| 31 | i18n / Localization | "No" default | UX, Development, QA |
| 32 | Uptime / SLA | "Best effort" default | Deployment |
| 33 | Documentation Deliverables | "User + API docs" default | All twins (adds per-phase documentation hours) |
| 34 | Post-Launch Support | "None" default | Deployment, QA |

#### Stage 3 (All Optional — Defaults to Conservative)
| # | Field | Default if Skipped | Consumed By |
|---|---|---|---|
| 35-40 | AI Maturity (6 phases) | Level 1 (No AI) for all | Corresponding twin + Orchestrator |
| 41 | Sr. Product Engineer % | 25% (preset "Balanced") | All twins (role attribution per phase) |
| 42 | Jr. Product Engineer % | 25% (preset "Balanced") | All twins (role attribution per phase) |
| 43 | Sr. Technical Engineer % | 25% (preset "Balanced") | All twins (role attribution per phase) |
| 44 | Jr. Technical Engineer % | 25% (preset "Balanced") | All twins (role attribution per phase) |
| 45 | Team Domain Experience | 3/5 (moderate) | COCOMO AEXP factor for all twins |
| 46 | Team Platform Experience | 3/5 (moderate) | COCOMO PEXP factor for Development, Deployment |
| 47 | Team Continuity Risk | "Stable" default | Orchestrator (risk contingency) |
| 48 | Process Maturity | 3/5 (defined methodology) | COCOMO PMAT factor for all twins |

#### Stage 4 (Dynamic — 5-10 Questions Generated Per Estimate)
| # | Field | Default if Skipped | Consumed By |
|---|---|---|---|
| 49-58 | Clarifying questions | AI-suggested defaults (stated in output) | Requesting twin(s) |

**Total: 48 structured fields + up to 10 dynamic questions**
**Minimum required to generate an estimate: 1 field (raw project description)**

---

## 3. Twin Agent Specifications

### 3.0 Dual-Output Framework: AI-Assisted vs. Manual Estimation

Every twin produces **two complete sets of effort estimates** for each phase:

1. **AI-Assisted Estimate** — applies the per-role AI effectiveness reductions from Section 2.4.1B, capped by the AI maturity ceiling from Section 2.4.1. This is the "recommended" estimate assuming the team actually adopts AI tooling at the specified maturity level.

2. **Manual-Only Estimate** — sets all AI reductions to 0% across all roles. This represents the traditional effort baseline with no AI assistance. It serves as the counterfactual: "what would this cost without AI?"

**Why dual outputs matter:**
- **Value quantification:** The delta between AI-assisted and manual hours, multiplied by role-specific billing rates, produces a concrete dollar figure for AI's value. This is a powerful sales tool for AI-forward engagements ("AI saves $185,000 on this project").
- **Risk hedging:** If AI adoption stalls mid-project (team resistance, tool limitations, compliance restrictions), stakeholders can see exactly what the fallback cost is.
- **Senior resource impact visibility:** Because senior engineers command 2–3× junior rates, AI effectiveness on senior hours has an outsized dollar impact. A 30% AI reduction on 500 senior hours at $250/hr saves $37,500; the same reduction on 500 junior hours at $120/hr saves $18,000. The dual output makes this asymmetry explicit per phase.

**Per-Role AI Reduction Calculation:**

Each twin calculates role-specific AI reductions using:

```
Effective_AI_Reduction[role][phase] = min(
    AI_effectiveness_rating_to_percentage(ai_role_effectiveness[phase][role]),
    maturity_ceiling(ai_maturity[phase])
)
```

Where:
- `AI_effectiveness_rating_to_percentage`: None=0%, Low=7.5%, Medium=20%, High=32.5%, Very High=47.5% (midpoints of ranges from Section 2.4.1B)
- `maturity_ceiling`: Level 1=0%, Level 2=10%, Level 3=30%, Level 4=45%, Level 5=55%

Each twin applies this reduction to the role's proportional hours within that phase:

```
AI_Assisted_Hours[role] = Manual_Hours[role] × (1 - Effective_AI_Reduction[role][phase])
```

**Output Schema (per twin):**

```json
{
  "phase": "development",
  "manual_estimate": {
    "total": {"low": 3100, "mid": 4020, "high": 5230},
    "by_role": {
      "sr_product": {"low": 465, "mid": 603, "high": 785},
      "jr_product": {"low": 775, "mid": 1005, "high": 1308},
      "sr_technical": {"low": 1240, "mid": 1608, "high": 2092},
      "jr_technical": {"low": 620, "mid": 804, "high": 1046}
    }
  },
  "ai_assisted_estimate": {
    "total": {"low": 2320, "mid": 3010, "high": 3915},
    "by_role": {
      "sr_product": {"low": 302, "mid": 392, "high": 510},
      "jr_product": {"low": 581, "mid": 754, "high": 981},
      "sr_technical": {"low": 806, "mid": 1045, "high": 1360},
      "jr_technical": {"low": 496, "mid": 643, "high": 837}
    }
  },
  "ai_delta": {
    "hours_saved": {"low": 780, "mid": 1010, "high": 1315},
    "cost_saved": {"low": 156000, "mid": 202000, "high": 263000},
    "percentage_reduction": "25.1%",
    "by_role_impact": {
      "sr_product": {"hours_saved": 211, "cost_saved_at_rate": 52750, "reduction_pct": "35.0%"},
      "jr_product": {"hours_saved": 251, "cost_saved_at_rate": 30120, "reduction_pct": "25.0%"},
      "sr_technical": {"hours_saved": 563, "cost_saved_at_rate": 140750, "reduction_pct": "35.0%"},
      "jr_technical": {"hours_saved": 161, "cost_saved_at_rate": 19320, "reduction_pct": "20.0%"}
    }
  }
}
```

The Orchestrator aggregates these dual outputs across all six twins and presents a combined view in Stage 5 (Section 5) with toggle controls for switching between AI-assisted and manual scenarios.

### 3.1 Discovery Analyst

#### 3.1.1 Evaluation Dimensions

- Stakeholder complexity (number of groups, decision-maker accessibility, alignment difficulty)
- Process documentation maturity (existing docs, tribal knowledge risk, system documentation availability)
- Domain complexity (regulatory requirements, industry-specific business rules, compliance overhead)
- System integration landscape (number of integration points, API availability, data format complexity)
- AI-assisted completeness checking (effort to run AI gap analysis, expected downstream rework reduction)
- Requirement ambiguity risk (scope definition clarity, client's ability to articulate needs)

#### 3.1.2 Formal Estimation Algorithm: Use Case Points (UCP) for Discovery Sizing

The Discovery Analyst uses a modified **Use Case Points (UCP)** method to size the requirements gathering effort. UCP was originally proposed by Gustav Karner (1993) and provides a function-point-like sizing metric grounded in use case complexity — ideal for the discovery phase where features are expressed as user workflows rather than code.

**Step 1 — Unadjusted Use Case Weight (UUCW):**

Classify each identified use case by transaction count:

| Use Case Complexity | Transaction Count | Weight |
|---|---|---|
| Simple | ≤ 3 transactions | 5 |
| Average | 4–7 transactions | 10 |
| Complex | > 7 transactions | 15 |

`UUCW = Σ (count_simple × 5) + (count_average × 10) + (count_complex × 15)`

**Step 2 — Unadjusted Actor Weight (UAW):**

Classify each actor by interaction complexity:

| Actor Type | Description | Weight |
|---|---|---|
| Simple | External system via API | 1 |
| Average | External system via protocol (e.g., TCP/IP, MQ) or human via structured interface | 2 |
| Complex | Human via rich GUI or multiple interaction channels | 3 |

`UAW = Σ (count_simple × 1) + (count_average × 2) + (count_complex × 3)`

**Step 3 — Technical Complexity Factor (TCF):**

`TCF = 0.6 + (0.01 × TFactor)`

Where `TFactor = Σ (weight_i × rating_i)` for 13 technical factors (distributed systems, performance, portability, reusability, security, etc.), each rated 0–5 by the twin based on project context.

**Step 4 — Environmental Complexity Factor (ECF):**

`ECF = 1.4 + (-0.03 × EFactor)`

Where `EFactor = Σ (weight_i × rating_i)` for 8 environmental factors (team experience, process maturity, analyst capability, requirements stability, etc.), each rated 0–5.

**Step 5 — Adjusted Use Case Points:**

`UCP = (UUCW + UAW) × TCF × ECF`

**Step 6 — Discovery Effort Derivation:**

Discovery effort is a phase-proportion of total project effort. Using Capers Jones's empirical benchmarks, requirements/discovery typically consumes 7–12% of total project effort:

`Discovery_Effort_Hours = UCP × Productivity_Factor × Phase_Ratio`

Where:
- `Productivity_Factor` = 20–28 hours/UCP (industry standard range; the twin selects within this range based on domain complexity and team experience from ECF)
- `Phase_Ratio` = 0.07–0.12 (proportion allocated to discovery; twin adjusts based on project type — regulated industries and legacy replacements skew toward 0.12, greenfield web apps toward 0.07)

**Step 7 — Stakeholder Complexity Adjustment:**

The UCP method doesn't account for stakeholder dynamics, which dominate discovery effort. The twin applies a multiplicative adjustment:

`Adjusted_Discovery_Hours = Discovery_Effort_Hours × Stakeholder_Multiplier`

| Stakeholder Factor | Condition | Multiplier |
|---|---|---|
| Group count | 1–2 groups | 1.0 |
| Group count | 3–5 groups | 1.15 |
| Group count | 6+ groups | 1.35 |
| Decision-maker accessibility | Readily available | 1.0 |
| Decision-maker accessibility | Gatekeeper or scheduling friction | 1.2 |
| Decision-maker accessibility | Multi-timezone or executive-level only | 1.4 |
| Alignment difficulty | Pre-aligned stakeholders | 1.0 |
| Alignment difficulty | Competing priorities or political dynamics | 1.25 |

Multipliers are compounded: e.g., 5 stakeholder groups (1.15) × gatekeeper access (1.2) × competing priorities (1.25) = 1.725× base discovery effort.

**Supplementary Method — Wideband Delphi Calibration:**

When the twin's confidence in use case enumeration is low (e.g., vague raw input with no structured Stage 2 data), it falls back to a **Wideband Delphi**-inspired approach: it generates three independent internal estimates using different scoping assumptions (optimistic, most-likely, pessimistic) and applies the PERT three-point formula:

`E = (O + 4M + P) / 6`

`σ = (P - O) / 6`

This produces a statistically grounded effort range with explicit uncertainty bounds.

#### 3.1.3 Worked Example: Healthcare Patient Portal

**Scenario:** Mid-size healthcare patient portal — enterprise client (2,000 employees), regulated industry (HIPAA), 4 stakeholder groups (clinical, IT, compliance, executive), integrations with 3 external systems (EHR, insurance API, lab results). The raw input describes appointment scheduling, patient records, secure messaging, lab result viewing, and billing.

**Step 1 — UUCW:**
- Simple use cases (≤3 transactions): Login, View Profile, View Lab Results = 3 × 5 = **15**
- Average use cases (4–7 transactions): Appointment Scheduling, Secure Messaging, Billing Summary = 3 × 10 = **30**
- Complex use cases (>7 transactions): Patient Record Management (CRUD + versioning + audit), Insurance Claim Flow = 2 × 15 = **30**
- `UUCW = 15 + 30 + 30 = 75`

**Step 2 — UAW:**
- Simple actors (API): EHR system, Insurance API, Lab Results API = 3 × 1 = **3**
- Average actors: Internal admin via structured interface = 1 × 2 = **2**
- Complex actors: Patient (rich GUI), Clinician (multi-channel) = 2 × 3 = **6**
- `UAW = 3 + 2 + 6 = 11`

**Step 3 — TCF:**
- TFactor (13 technical factors scored): distributed system (4), performance (3), security (5), reusability (3), others moderate → TFactor = **42**
- `TCF = 0.6 + (0.01 × 42) = 1.02`

**Step 4 — ECF:**
- EFactor (8 environmental factors): moderate team experience (3), stable requirements (2), good analyst capability (4), others moderate → EFactor = **24**
- `ECF = 1.4 + (-0.03 × 24) = 0.68`

**Step 5 — UCP:**
- `UCP = (75 + 11) × 1.02 × 0.68 = 86 × 0.694 = 59.6 ≈ 60 UCP`

**Step 6 — Discovery Effort:**
- Productivity Factor: 24 hrs/UCP (mid-range, enterprise healthcare context)
- Phase Ratio: 0.10 (regulated industry, pushed toward upper range)
- `Discovery_Effort_Hours = 60 × 24 × 0.10 = 144 hours`

**Step 7 — Stakeholder Adjustment:**
- 4 stakeholder groups → 1.15
- Gatekeeper access (executive sign-off required) → 1.2
- Moderate alignment difficulty → 1.0
- `Adjusted_Discovery_Hours = 144 × 1.15 × 1.2 × 1.0 = 198.7 ≈ 199 hours`

**PERT Three-Point Result:**
- Optimistic (O): 155 hrs (lower productivity factor, fewer iterations)
- Most Likely (M): 199 hrs
- Pessimistic (P): 268 hrs (higher phase ratio, more stakeholder friction)
- `Expected = (155 + 4×199 + 268) / 6 = 203 hours`
- `σ = (268 - 155) / 6 = 18.8 hours`

**Manual-Only Estimate: Low = 155 hrs | Mid = 203 hrs | High = 268 hrs (±19 hrs confidence interval)**

**Dual Output — Role Breakdown and AI Impact:**

Role percentages for Discovery phase (twin-adjusted from project-level 25/25/25/25 — Discovery skews toward product roles): Sr. Product 40%, Jr. Product 20%, Sr. Technical 25%, Jr. Technical 15%.

| Role | Manual Hours (Mid) | AI Effectiveness | Maturity Cap (L3=30%) | Effective Reduction | AI-Assisted Hours |
|---|---|---|---|---|---|
| Sr. Product Eng. | 81 hrs | High (32.5%) | 30% | **30%** | 57 hrs |
| Jr. Product Eng. | 41 hrs | Low (7.5%) | 30% | **7.5%** | 38 hrs |
| Sr. Technical Eng. | 51 hrs | Medium (20%) | 30% | **20%** | 41 hrs |
| Jr. Technical Eng. | 30 hrs | Low (7.5%) | 30% | **7.5%** | 28 hrs |
| **Total** | **203 hrs** | | | | **164 hrs** |

**AI Delta:** 39 hours saved (19.2% reduction). At blended rates: ~$7,440 saved. Senior roles account for 73% of hours saved (seniors: 34 hrs saved vs. juniors: 5 hrs saved) due to higher AI effectiveness on senior discovery work.

**Final Dual Output:**

| Scenario | Low | Mid | High |
|---|---|---|---|
| **Manual-Only** | 155 hrs | 203 hrs | 268 hrs |
| **AI-Assisted** | 125 hrs | 164 hrs | 217 hrs |
| **AI Delta** | 30 hrs saved | 39 hrs saved | 51 hrs saved |

#### 3.1.4 Inference Rules

- Regulated industry → increase stakeholder complexity and compliance discovery effort; push Phase_Ratio toward 0.12
- Legacy system replacement → increase process documentation effort, flag tribal knowledge risk; add 15–25% for reverse-engineering existing undocumented behavior
- Multiple integration points mentioned → add per-integration discovery overhead (4–8 hrs per integration point for API assessment and data mapping)
- Vague scope description → fall back to Wideband Delphi three-point estimation; increase overall discovery estimate and flag high ambiguity risk
- Prior project data available → calibrate Productivity_Factor using historical UCP-to-hours data

#### 3.1.4 Output

- Effort range in hours (low/mid/high) derived from UCP calculation with uncertainty bounds (±σ)
- UCP sizing breakdown: use case count by complexity, actor count by type, TCF and ECF values used
- Stakeholder multiplier applied with justification
- Assumptions list with confidence levels
- Risk flags with potential effort impact if assumptions are wrong
- Dependency notes for downstream twins (e.g., "discovery completeness rating: medium — UX and Dev twins should account for potential spec gaps")

### 3.2 UX/Design Strategist

#### 3.2.1 Evaluation Dimensions

- Number of distinct user roles and workflows requiring unique design
- AI-generatable percentage (how much of the UX can be produced from a strong spec sheet versus requiring original human design)
- Design system availability (existing client brand guidelines, component libraries, or starting from scratch)
- Interaction complexity (standard CRUD interfaces versus novel interaction patterns, data visualization, complex workflows)
- Client design iteration appetite (how many review cycles to expect)
- Accessibility requirements (WCAG compliance level, specific accommodation needs)

#### 3.2.2 Formal Estimation Algorithm: Screen Complexity Weighting with FPA Correlation

The UX/Design Strategist uses a **Screen Complexity Point (SCP)** method that correlates with IFPUG Function Point Analysis (FPA) transaction functions. This bridges the gap between functional sizing (what the system does) and design sizing (what the user sees and interacts with).

**Step 1 — Screen Inventory from Function Points:**

Map the project's EI (External Input), EO (External Output), and EQ (External Inquiry) transaction functions to UI screens/views. Each unique screen gets a complexity rating:

| Screen Complexity | Criteria | Base Hours |
|---|---|---|
| Simple | ≤ 3 data fields, single action, standard CRUD pattern (e.g., settings page, simple form) | 2–4 hrs |
| Average | 4–8 data fields, 2–3 actions, conditional logic or multi-step flow (e.g., search/filter, dashboard widget) | 6–10 hrs |
| Complex | 9+ data fields, rich interactions (drag-and-drop, real-time updates, data visualization, multi-panel layout) | 12–20 hrs |
| Novel | Unprecedented interaction pattern, custom visualization, animation-heavy, or no design system precedent | 20–40 hrs |

`Raw_Screen_Points = Σ (count_simple × 3) + (count_average × 8) + (count_complex × 16) + (count_novel × 30)`

**Step 2 — Design System Factor (DSF):**

The availability of an existing design system dramatically affects per-screen effort:

| Design System State | Factor |
|---|---|
| Mature design system with full component library (e.g., established MUI/Ant Design customization) | 0.5 |
| Partial design system — core components exist, but needs extension | 0.7 |
| Third-party library adopted but not customized | 0.85 |
| No design system — building from scratch | 1.0 |
| No design system + brand identity work required | 1.3 |

**Step 3 — Interaction Complexity Multiplier (ICM):**

Derived from the KLM-GOMS model concept, this captures the interaction density beyond simple screen counts:

| Interaction Pattern | Multiplier |
|---|---|
| Standard forms and lists (CRUD) | 1.0 |
| Multi-step wizards or guided flows | 1.15 |
| Real-time collaborative features | 1.4 |
| Data visualization / charting dashboards | 1.3 |
| Drag-and-drop or spatial interfaces | 1.35 |
| Accessibility-first design (WCAG AA+) | 1.2 (compounded) |

**Step 4 — Iteration Factor (IF):**

Client review cycles are a major effort driver in UX. The twin estimates iteration rounds based on client profile:

| Client Profile | Expected Iterations | Factor |
|---|---|---|
| Technical stakeholders, agile process, fast decisions | 2 rounds | 1.0 |
| Mixed stakeholders, moderate decision speed | 3 rounds | 1.3 |
| Non-technical stakeholders, committee-based decisions | 4–5 rounds | 1.6 |
| Highly regulated or compliance-driven (each iteration requires sign-off) | 5+ rounds | 2.0 |

**Step 5 — AI-Assisted Design Reduction:**

Based on the AI maturity assessment (Stage 3) and discovery output quality:

| AI Maturity Level | Discovery Quality | AI Design Reduction |
|---|---|---|
| Level 4–5 | Strong (rated "high" by Discovery twin) | 25–40% effort reduction on Simple/Average screens |
| Level 3 | Strong | 15–25% reduction |
| Level 3+ | Weak (rated "low/medium" by Discovery twin) | 5–10% reduction (AI needs good specs to generate good designs) |
| Level 1–2 | Any | 0% reduction |

`AI_Reduction_Factor = 1.0 - applicable_reduction_percentage`

**Step 6 — Total UX Effort:**

```
UX_Effort = Raw_Screen_Points × DSF × ICM × IF × AI_Reduction_Factor
```

**Step 7 — Activity Breakdown:**

The total UX effort is distributed across design activities using empirical ratios:

| Activity | Percentage of Total |
|---|---|
| User research & persona definition | 10–15% |
| Information architecture & flow mapping | 10–15% |
| Wireframing (low-fidelity) | 15–20% |
| Visual design (high-fidelity mockups) | 25–35% |
| Prototyping & interaction specification | 15–20% |
| Design review & iteration | 10–15% (already factored via IF, so this captures handoff/documentation overhead) |

#### 3.2.3 Worked Example: Healthcare Patient Portal

**Scenario (continued):** Same healthcare patient portal. The Discovery Analyst rated discovery completeness "medium-high." The client has a partial design system (customized MUI). The portal is patient-facing (consumer-grade UX expectations). AI maturity Level 3. Two user roles (Patient, Clinician) plus an Admin role.

**Step 1 — Screen Inventory:**
- Simple screens: Login, Forgot Password, Profile View, Settings, Lab Result Detail = 5 × 3 hrs = **15 hrs**
- Average screens: Appointment Calendar, Messaging Inbox, Message Thread, Billing Summary, Insurance Card, Notification Center, Search Results = 7 × 8 hrs = **56 hrs**
- Complex screens: Patient Dashboard (multi-panel, real-time), Patient Record (tabbed, versioned data), Appointment Booking Flow (multi-step wizard), Clinician Dashboard = 4 × 16 hrs = **64 hrs**
- Novel screens: Secure Video Consultation Interface = 1 × 30 hrs = **30 hrs**
- `Raw_Screen_Points = 15 + 56 + 64 + 30 = 165 hours`

**Step 2 — Design System Factor:**
- Partial design system (customized MUI) → DSF = **0.7**

**Step 3 — Interaction Complexity Multiplier:**
- Mix: mostly standard forms/lists (1.0) but includes multi-step wizard (1.15), real-time dashboard (1.3), and WCAG AA required (1.2 compounded)
- Weighted average across screens: ICM ≈ **1.18**

**Step 4 — Iteration Factor:**
- Healthcare client: mixed stakeholders (clinical + IT + compliance), committee-based decisions for patient-facing UI → IF = **1.6** (4–5 review rounds expected)

**Step 5 — AI-Assisted Design Reduction:**
- AI Maturity Level 3 + Discovery quality "medium-high" → 20% reduction on Simple/Average screens
- Simple + Average = 71 hrs; 20% of 71 = 14.2 hrs saved
- Complex/Novel screens: 5% reduction → 94 hrs × 0.05 = 4.7 hrs saved
- Total savings: 18.9 hrs → `AI_Reduction_Factor = 1.0 - (18.9 / 165) = 0.885`

**Step 6 — Total UX Effort:**
- `UX_Effort = 165 × 0.7 × 1.18 × 1.6 × 0.885 = 165 × 0.7 × 1.18 × 1.6 × 0.885`
- `= 165 × 0.7 = 115.5`
- `= 115.5 × 1.18 = 136.3`
- `= 136.3 × 1.6 = 218.1`
- `= 218.1 × 0.885 = 193.0 hours`

**Step 7 — Activity Breakdown:**

| Activity | % | Hours |
|---|---|---|
| User research & persona definition | 12% | 23 hrs |
| Information architecture & flow mapping | 13% | 25 hrs |
| Wireframing (low-fidelity) | 18% | 35 hrs |
| Visual design (high-fidelity mockups) | 30% | 58 hrs |
| Prototyping & interaction specification | 17% | 33 hrs |
| Design review & iteration overhead | 10% | 19 hrs |

**Mobile-Responsive Addition:**
- Patient-facing portal requires responsive design → add 35% to base: 193 × 0.35 = 67.6 hrs
- `Total with responsive = 193 + 68 = 261 hours`

**PERT Three-Point Result:**
- Optimistic (O): 198 hrs (higher AI reduction, fewer iterations)
- Most Likely (M): 261 hrs
- Pessimistic (P): 348 hrs (novel video consultation scope creep, more iterations)
- `Expected = (198 + 4×261 + 348) / 6 = 265 hours`
- `σ = (348 - 198) / 6 = 25 hours`

**Manual-Only Estimate: Low = 198 hrs | Mid = 265 hrs | High = 348 hrs (±25 hrs confidence interval)**

Note: The manual estimate above already excludes the AI design reduction from Step 5. For the true manual baseline, remove the AI_Reduction_Factor (0.885) from Step 6:

`Manual_UX_Effort = 165 × 0.7 × 1.18 × 1.6 = 218.1 hrs` + responsive 35% = **295 hrs (Mid manual baseline)**

**Dual Output — Role Breakdown and AI Impact:**

Role percentages for UX/Design phase (twin-adjusted — UX heavily skews product): Sr. Product 45%, Jr. Product 30%, Sr. Technical 15%, Jr. Technical 10%.

| Role | Manual Hours (Mid) | AI Effectiveness | Maturity Cap (L3=30%) | Effective Reduction | AI-Assisted Hours |
|---|---|---|---|---|---|
| Sr. Product Eng. | 133 hrs | High (32.5%) | 30% | **30%** | 93 hrs |
| Jr. Product Eng. | 88 hrs | Medium (20%) | 30% | **20%** | 70 hrs |
| Sr. Technical Eng. | 44 hrs | Low (7.5%) | 30% | **7.5%** | 41 hrs |
| Jr. Technical Eng. | 30 hrs | Low (7.5%) | 30% | **7.5%** | 28 hrs |
| **Total** | **295 hrs** | | | | **232 hrs** |

**AI Delta:** 63 hours saved (21.4% reduction). At blended rates: ~$13,930 saved. Senior Product Engineers account for 63% of hours saved (40 hrs) — their ability to direct AI design tools effectively (prompt-driven wireframe generation, AI-assisted visual design iteration) creates the largest efficiency gain.

**Final Dual Output:**

| Scenario | Low | Mid | High |
|---|---|---|---|
| **Manual-Only** | 224 hrs | 295 hrs | 392 hrs |
| **AI-Assisted** | 176 hrs | 232 hrs | 308 hrs |
| **AI Delta** | 48 hrs saved | 63 hrs saved | 84 hrs saved |

#### 3.2.4 Inference Rules

- Strong discovery output (from Discovery Analyst) → higher AI design reduction, lower effort; push AI_Reduction_Factor down
- Weak or incomplete discovery output → more human design time, increase IF by one tier
- Consumer-facing application → higher design bar, classify more screens as Complex/Novel, push ICM up
- Internal tool → potentially simpler design, more screens classify as Simple/Average, higher AI-generatable percentage
- Mobile-responsive requirement → add 30–40% to base screen points (each screen effectively requires two designs)
- Multi-platform (web + native mobile) → multiply screen inventory by platform count with 60% reuse factor

#### 3.2.4 Output

- Effort range in hours (low/mid/high), split between AI-assisted generation and human design time
- Screen inventory with complexity classification (simple/average/complex/novel counts)
- Design System Factor, Interaction Complexity Multiplier, and Iteration Factor values used with justification
- AI design reduction percentage applied
- Activity breakdown (research, wireframing, visual design, prototyping, review)
- Assumptions about discovery output quality and its impact on design effort
- Risk flags (e.g., "if the client requires custom illustration or motion design, add X hours")
- Dependency notes (design artifacts directly feed Development Architect's estimate)

### 3.3 Development Architect

The Development Architect is the most complex twin because the effort variance between tech stacks is enormous — a React/Node.js greenfield API might take 200 hours while the same functional scope on a legacy COBOL modernization could take 1,200. This twin must reason across a wide taxonomy of technology environments and apply stack-specific multipliers, not generic "complexity" scores. It must also assess what existing infrastructure, frameworks, libraries, and services the client already has in place and calculate the cost delta between building from scratch versus leveraging what exists.

#### 3.3.1 Evaluation Dimensions

**A. Tech Stack Assessment**

The twin maintains an internal taxonomy of technology categories with associated effort profiles. When the raw input or Stage 2 structured fields specify a tech stack, the twin classifies it and applies the corresponding profile. When the stack is unknown, it infers from contextual clues (industry, client size, project age, platform targets).

| Category | Examples | Effort Profile | AI-Assisted Dev Effectiveness |
|---|---|---|---|
| **Modern Web (JS/TS ecosystem)** | React, Next.js, Vue, Angular, Node.js, Express, Nest.js, Remix, Svelte | Baseline — well-documented, strong AI training data, rich ecosystem of packages and templates | High — AI coding tools perform best here due to abundant training data |
| **Modern Web (Python ecosystem)** | Django, FastAPI, Flask, Celery, SQLAlchemy | Baseline to 1.1x — strong ecosystem, slightly less frontend AI assistance than JS/TS | High — Python has excellent AI tool coverage |
| **Enterprise Java / JVM** | Spring Boot, Quarkus, Micronaut, Kotlin, Scala | 1.1-1.3x baseline — more boilerplate, stricter typing, enterprise patterns add overhead but also reduce ambiguity | Medium-High — good AI coverage for Spring, less for niche JVM frameworks |
| **Microsoft / .NET** | ASP.NET Core, Blazor, C#, Entity Framework, Azure Functions | 1.1-1.3x baseline — well-supported ecosystem, but licensing and Azure integration patterns add configuration overhead | Medium-High — strong Copilot integration, good training data |
| **Mobile Native** | Swift/SwiftUI (iOS), Kotlin/Jetpack Compose (Android) | 1.3-1.6x baseline per platform — platform-specific APIs, App Store review processes, device fragmentation testing | Medium — AI tools handle UI components well, struggle with platform-specific edge cases |
| **Mobile Cross-Platform** | React Native, Flutter, .NET MAUI, Kotlin Multiplatform | 1.1-1.4x baseline — code sharing reduces total effort, but platform bridging and native module needs add complexity | Medium — Flutter/RN have decent AI coverage, MAUI and KMP are weaker |
| **Legacy Web** | JSP, Struts, ColdFusion, Classic ASP, PHP 5.x, jQuery-heavy monoliths | 1.5-2.5x baseline — poor documentation, limited AI training data, manual processes, brittle test infrastructure | Low — AI tools have minimal useful training data for legacy frameworks |
| **Legacy Enterprise** | COBOL, RPG, Mainframe, SAP ABAP, Oracle Forms, PowerBuilder | 2.0-4.0x baseline — specialized knowledge required, extremely limited AI assistance, migration patterns are complex and high-risk | Very Low — AI tools are effectively useless for these stacks |
| **Data / ML Platform** | Spark, Airflow, dbt, Snowflake, Databricks, MLflow, Kubeflow | 1.2-1.5x baseline — specialized infrastructure, data pipeline testing is harder than application testing, model lifecycle management adds scope | Medium — AI assists with boilerplate ETL but struggles with domain-specific data logic |
| **Infrastructure / Platform** | Terraform, Pulumi, Kubernetes manifests, Helm, CDK, CloudFormation | 1.1-1.4x baseline — declarative config is AI-friendly, but the blast radius of errors is high so human review is more critical | Medium-High — IaC has good AI tool support but requires careful validation |
| **Embedded / IoT** | C/C++, Rust, Arduino, RTOS, firmware | 1.5-2.5x baseline — hardware dependencies, cross-compilation, limited debugging tools, safety-critical testing requirements | Low — AI tools have limited embedded-specific training data |
| **Blockchain / Web3** | Solidity, Rust (Solana), Move, smart contracts | 1.5-2.0x baseline — security-critical, audit requirements, limited tooling maturity, gas optimization complexity | Low-Medium — some AI coverage for Solidity, much less for newer chains |

**Stack combination multiplier:** Most real projects span multiple categories (e.g., React frontend + Spring Boot backend + Terraform infrastructure). The twin estimates effort per layer independently and sums them, rather than applying a single blended multiplier. Cross-layer integration effort (API contracts, deployment coordination, shared auth) is estimated separately.

**B. Existing Infrastructure & Framework Leverage**

This is the critical "build vs. reuse" assessment that the current plan was missing. The twin evaluates what the client already has in place and calculates the effort reduction from leveraging it versus the effort cost of building from scratch:

| Infrastructure Element | If Exists & Reusable | If Must Build New | Effort Delta |
|---|---|---|---|
| **Authentication / Identity** | SSO/OAuth already integrated (Okta, Auth0, Azure AD, Cognito) — configure and connect: 8-24 hrs | Build auth from scratch or integrate a new identity provider: 40-120 hrs | 2-5x reduction |
| **Design System / Component Library** | Existing component library (MUI, Ant Design, custom) with established patterns — compose and extend: minimal per-screen overhead | Build from scratch or adopt and customize a new library: 40-80 hrs setup + per-component cost | 1.5-3x reduction on frontend effort |
| **CI/CD Pipeline** | Existing pipelines (GitHub Actions, GitLab CI, Jenkins, Azure DevOps) that cover build/test/deploy — adapt for new project: 8-16 hrs | Greenfield pipeline setup including quality gates, environments, and deployment targets: 40-80 hrs | 3-5x reduction |
| **API Gateway / BFF Layer** | Existing gateway (Kong, Apigee, AWS API Gateway) with routing, rate limiting, auth — register new routes: 4-8 hrs per endpoint group | Stand up a new gateway or build direct integrations: 24-60 hrs | 3-6x reduction |
| **Database / Data Layer** | Existing database with migrations framework, connection pooling, backup strategy — extend schema: moderate per-entity | Provision new database, set up migrations, backup, connection management: 16-40 hrs setup | 2-3x reduction on setup |
| **Monitoring / Observability** | Existing APM (Datadog, New Relic, Grafana stack), log aggregation, alerting — instrument new services: 4-8 hrs | Build observability from scratch: 24-60 hrs | 3-5x reduction |
| **Message Queue / Event Bus** | Existing Kafka, RabbitMQ, SQS with established patterns — publish/subscribe to new topics: 4-12 hrs per integration | Stand up new messaging infrastructure: 24-48 hrs setup + per-integration | 2-4x reduction |
| **Caching Layer** | Existing Redis/Memcached with established patterns — add new cache keys: 2-4 hrs per feature | Implement caching strategy from scratch: 16-32 hrs | 3-5x reduction |
| **File Storage / CDN** | Existing S3/Blob Storage/CloudFront with upload/download patterns — reuse: 2-8 hrs | Set up new storage, CDN, access policies: 16-32 hrs | 2-4x reduction |
| **Feature Flags / Experimentation** | Existing LaunchDarkly, Unleash, or custom feature flag system — wrap new features: 1-2 hrs per flag | Implement feature flag system: 16-40 hrs | 5-10x reduction per feature |
| **Shared Libraries / Internal Packages** | Client has reusable internal libraries (validation, formatting, domain models) — import and use: minimal | Write from scratch: varies widely by library scope | Variable — can save 20-40% of utility code effort |
| **Testing Infrastructure** | Existing test runners, fixtures, factories, E2E framework (Cypress, Playwright) — extend: 4-8 hrs per new test suite | Set up test infrastructure from scratch: 24-60 hrs | 3-5x reduction on setup (feeds into QA twin) |

**Leverage scoring:** The twin assigns each infrastructure element one of four states:
- **Reusable as-is** — exists and requires only configuration for the new project (maximum effort reduction)
- **Adaptable** — exists but needs modification, extension, or version upgrade (partial effort reduction, 40-70% of full build)
- **Incompatible** — exists but can't be reused for the new project (no reduction, may add migration overhead if it needs to be replaced)
- **Absent** — doesn't exist, must be built (full build effort)

The twin's prompt instructs it to actively look for infrastructure leverage opportunities in the raw input and Stage 2 data — mentions of "we already use Datadog," "they have an existing API gateway," or "the client's team runs GitHub Actions" should trigger leverage scoring even if the user didn't explicitly flag them.

**C. Feature Complexity and Effort Estimation**

- Feature/epic count and estimated complexity per feature — classified by the taxonomy: CRUD operations (low), business logic with branching rules (medium), real-time/streaming features (high), AI/ML integration features (high), data transformation/migration logic (very high)
- Per-feature effort adjusted by the tech stack multiplier from the taxonomy above
- Per-feature effort further adjusted by the infrastructure leverage score — features that can reuse existing auth, existing APIs, existing component libraries cost less

**D. AI Development Readiness**

- AI-to-human development ratio (based on client AI maturity from Stage 3, project complexity, AND tech stack — the taxonomy specifies AI-assisted dev effectiveness per stack category)
- AI guardrail setup cost — varies by stack: JS/TS and Python ecosystems have mature AI coding tool integrations (low setup), legacy and embedded stacks may have no viable AI tooling (setup cost is N/A, ratio defaults to 0% AI)
- Team training overhead if the team's AI maturity is lower than what the project stack supports

**E. Integration Build Effort**

- Per-integration effort based on integration type: REST API (low), GraphQL (low-medium), SOAP/XML (medium), database-level (high), file-based/batch (medium), real-time streaming (high), legacy system with no API (very high — may require building an adapter layer)
- Existing integration middleware or iPaaS (MuleSoft, Boomi, Workato) can reduce per-integration effort by 40-60% if available

**F. Legacy Modernization Overhead**

- Strangler fig pattern (incremental replacement): 1.3-1.5x multiplier on affected features — build the new alongside the old, with routing/proxy layer
- Big bang rewrite: 1.0x multiplier on new build, but add data migration effort + parallel run validation (from QA twin)
- Lift-and-shift with modernization: 0.8-1.0x on infrastructure, but add refactoring effort per module
- Technical debt remediation scope — estimate hours to address debt that would block new feature development

#### 3.3.2 Formal Estimation Algorithm: COCOMO II Post-Architecture Model with Stack Multipliers

The Development Architect uses **COCOMO II** (Constructive Cost Model II, Boehm et al., 2000) as its core parametric engine, extended with the tech stack taxonomy multipliers and infrastructure leverage scoring defined above. COCOMO II is the most widely validated parametric model for development effort estimation, calibrated against thousands of projects.

**Step 1 — Size Estimation via Function Points → SLOC Conversion:**

The twin receives the Function Point count from the Discovery Analyst's UCP sizing (or derives it from the screen inventory and data model). It converts FP to SLOC using language-specific backfiring ratios:

| Language/Framework | SLOC per Function Point |
|---|---|
| JavaScript/TypeScript | 47 |
| Python | 32 |
| Java | 53 |
| C# | 54 |
| Swift | 40 |
| Kotlin | 40 |
| Go | 42 |
| C/C++ | 97 |
| COBOL | 107 |
| SQL | 21 |
| HCL (Terraform) | 30 |
| Dart (Flutter) | 38 |

For multi-stack projects, the twin computes SLOC per layer using the language breakdown and sums for total KSLOC:

`KSLOC = Σ (FP_layer × SLOC_per_FP_layer) / 1000`

**Step 2 — Scale Factor Exponent (E):**

`E = B + 0.01 × Σ SF_j` where `B = 0.91`

The five COCOMO II scale factors, each rated Very Low (6.20) to Extra High (0.00):

| Scale Factor | Abbreviation | What It Measures | Rating Source |
|---|---|---|---|
| Precedentedness | PREC | Team's familiarity with this type of project | Project type + client history from Stage 2 |
| Development Flexibility | FLEX | Degree of specification rigidity | Engagement model (Fixed Price = low flex, T&M = high flex) |
| Architecture/Risk Resolution | RESL | How much architecture is defined upfront | Existing codebase toggle + tech maturity signals |
| Team Cohesion | TEAM | Stakeholder/team alignment and communication | Team profile from Stage 3 |
| Process Maturity | PMAT | Organization's process maturity (CMMI-based) | Client organization size + process signals |

Example: `E = 0.91 + 0.01 × (3.72 + 2.03 + 4.24 + 3.29 + 4.68) = 0.91 + 0.1796 = 1.0896`

**Step 3 — Effort Adjustment Factor (EAF):**

`EAF = Π (EM_i)` — the product of 17 cost driver effort multipliers, each rated from Very Low to Extra High:

| Category | Cost Drivers |
|---|---|
| Product | RELY (reliability), DATA (database size), CPLX (complexity), RUSE (reusability), DOCU (documentation) |
| Platform | TIME (execution time constraint), STOR (storage constraint), PVOL (platform volatility) |
| Personnel | ACAP (analyst capability), PCAP (programmer capability), AEXP (application experience), PEXP (platform experience), LTEX (language/tool experience), PCON (personnel continuity) |
| Project | TOOL (use of software tools), SITE (multi-site development), SCED (required development schedule) |

The twin maps project context to these drivers:
- Team profile (Stage 3) → ACAP, PCAP, AEXP, PEXP, LTEX, PCON
- AI maturity level → adjusts TOOL rating upward (AI tools augment software tooling)
- Tech stack from taxonomy → adjusts CPLX (legacy stacks = higher complexity), PVOL (bleeding-edge stacks = higher platform volatility)
- Infrastructure leverage score → reduces effective SIZE (reusable infrastructure = less code to write)

**Step 4 — Base COCOMO II Effort:**

`PM_base = 2.94 × (KSLOC)^E × EAF`

Where PM is person-months. Convert to hours: `Hours_base = PM_base × 152` (assumes 152 productive hours per person-month).

**Step 5 — Tech Stack Multiplier Overlay:**

The COCOMO II base estimate assumes a "typical" modern stack. The twin applies the tech stack taxonomy multiplier (Section 3.3.1A) as an additional adjustment:

`Hours_stack_adjusted = Hours_base × Weighted_Stack_Multiplier`

Where `Weighted_Stack_Multiplier = Σ (layer_effort_fraction × stack_multiplier_for_layer)`

Example: Frontend (40% of effort, Modern Web JS, 1.0×) + Backend (45%, Enterprise Java, 1.2×) + Infra (15%, Infrastructure/Platform, 1.1×) → Weighted = 0.40(1.0) + 0.45(1.2) + 0.15(1.1) = 1.105×

**Step 6 — Infrastructure Leverage Reduction:**

Apply the infrastructure leverage scorecard (Section 3.3.1B) as an effort reduction:

`Hours_final = Hours_stack_adjusted × (1 - Leverage_Reduction)`

Where `Leverage_Reduction` is calculated from the infrastructure elements:
- Each "Reusable as-is" element: full effort delta saved (e.g., auth: save 32–96 hrs)
- Each "Adaptable" element: 40–70% of effort delta saved
- "Incompatible" or "Absent" elements: no reduction

The twin sums the saved hours across all 12 infrastructure elements and expresses the total as a percentage reduction of the base development effort.

**Step 7 — AI Development Effectiveness Adjustment:**

`Hours_AI_adjusted = Hours_final × (1 - AI_Reduction)`

Where `AI_Reduction` is derived from the AI maturity level × stack-specific AI effectiveness (Section 3.3.1D):

| AI Maturity × Stack AI Effectiveness | AI_Reduction |
|---|---|
| Level 4–5 × High effectiveness | 0.25–0.35 |
| Level 3 × High effectiveness | 0.15–0.25 |
| Level 3 × Medium effectiveness | 0.10–0.15 |
| Level 1–2 × Any | 0.00 |
| Any × Low/Very Low effectiveness | 0.00–0.05 |

**Step 8 — Three-Point Estimation (PERT):**

The twin runs the COCOMO II calculation three times with different parameter assumptions:
- **Optimistic (O):** favorable scale factors, high team capability, maximum infrastructure leverage
- **Most Likely (M):** median parameter values based on best interpretation of inputs
- **Pessimistic (P):** unfavorable scale factors, lower team capability, minimal leverage

`Effort_expected = (O + 4M + P) / 6`

`σ = (P - O) / 6`

Output: `Low = O`, `Mid = Effort_expected`, `High = P` with confidence interval `Mid ± σ`

**Supplementary Model — Putnam SLIM Cross-Check:**

For projects where KSLOC exceeds 50 (large systems), the twin runs a secondary estimate using the **Putnam SLIM** model as a sanity check:

`Effort = (Size / Productivity)^(1/0.4) × B^0.333`

Where `Size` is effective SLOC, `Productivity` is a technology-dependent constant from historical calibration, and `B` is the manpower buildup index. If the SLIM estimate diverges from COCOMO II by more than 30%, the twin flags the discrepancy and widens the effort range.

#### 3.3.3 Worked Example: Healthcare Patient Portal

**Scenario (continued):** Same portal. Tech stack: Next.js (frontend), Spring Boot 3 (backend), PostgreSQL, Terraform + Kubernetes on AWS. Enterprise client has existing auth (Okta), existing CI/CD (GitHub Actions), existing monitoring (Datadog), but no existing API gateway or feature flags. AI maturity Level 3. Total Function Points from Discovery: ~180 FP.

**Step 1 — Size Estimation (FP → SLOC):**
- Frontend (Next.js/TypeScript): 60% of features → 108 FP × 47 SLOC/FP = 5,076 SLOC
- Backend (Spring Boot/Java): 35% of features → 63 FP × 53 SLOC/FP = 3,339 SLOC
- Infrastructure (Terraform/HCL): 5% → 9 FP × 30 SLOC/FP = 270 SLOC
- `KSLOC = (5,076 + 3,339 + 270) / 1000 = 8.685 KSLOC`

**Step 2 — Scale Factor Exponent (E):**

| Scale Factor | Rating | Value | Rationale |
|---|---|---|---|
| PREC | Nominal (some similar projects) | 3.72 | Enterprise healthcare portal has precedents |
| FLEX | Low (HIPAA rigid) | 4.05 | Regulated industry, strict compliance |
| RESL | Nominal | 4.24 | Architecture partially defined |
| TEAM | Nominal | 3.29 | Mixed team, moderate cohesion |
| PMAT | Nominal (CMMI Level 2) | 4.68 | Enterprise process, not fully mature |

- `E = 0.91 + 0.01 × (3.72 + 4.05 + 4.24 + 3.29 + 4.68) = 0.91 + 0.1998 = 1.11`

**Step 3 — Effort Adjustment Factor (EAF):**

Key cost drivers (showing only non-nominal ones):

| Cost Driver | Rating | Multiplier | Rationale |
|---|---|---|---|
| RELY (reliability) | High | 1.10 | Healthcare — patient data, HIPAA |
| CPLX (complexity) | High | 1.17 | Multiple integrations, compliance logic |
| RUSE (reusability) | Nominal | 1.00 | — |
| ACAP (analyst capability) | High | 0.85 | Strong team |
| PCAP (programmer capability) | High | 0.88 | Experienced developers |
| PEXP (platform experience) | Nominal | 1.00 | — |
| LTEX (language/tool experience) | High | 0.91 | Team knows Java + TypeScript well |
| TOOL (tool use) | High | 0.90 | AI tooling at Level 3 + good IDE support |
| SCED (schedule) | Nominal | 1.00 | — |
| Others (8 drivers) | Nominal | 1.00 each | — |

- `EAF = 1.10 × 1.17 × 0.85 × 0.88 × 0.91 × 0.90 = 0.790`

**Step 4 — Base COCOMO II Effort:**
- `PM_base = 2.94 × (8.685)^1.11 × 0.790`
- `(8.685)^1.11 = 10.91` (exponential scaling)
- `PM_base = 2.94 × 10.91 × 0.790 = 25.33 person-months`
- `Hours_base = 25.33 × 152 = 3,850 hours`

**Step 5 — Tech Stack Multiplier:**
- Frontend (40% of effort, Modern Web JS, 1.0×) = 0.40 × 1.0 = 0.40
- Backend (45% of effort, Enterprise Java, 1.2×) = 0.45 × 1.2 = 0.54
- Infrastructure (15% of effort, Infrastructure/Platform, 1.1×) = 0.15 × 1.1 = 0.165
- `Weighted_Stack_Multiplier = 0.40 + 0.54 + 0.165 = 1.105`
- `Hours_stack_adjusted = 3,850 × 1.105 = 4,254 hours`

**Step 6 — Infrastructure Leverage Reduction:**

| Element | Status | Hours Saved |
|---|---|---|
| Authentication (Okta) | Reusable | 80 hrs (vs. 96 hrs to build) |
| CI/CD (GitHub Actions) | Reusable | 56 hrs (vs. 64 hrs to build) |
| Monitoring (Datadog) | Reusable | 48 hrs (vs. 52 hrs to build) |
| Database (PostgreSQL exists) | Adaptable (60% saved) | 19 hrs (vs. 32 hrs setup) |
| API Gateway | Absent | 0 hrs saved |
| Feature Flags | Absent | 0 hrs saved |
| Design System (partial MUI) | Adaptable (50% saved) | 30 hrs |
| Testing Infra | Adaptable (50% saved) | 20 hrs |

- Total hours saved: 253 hrs
- `Leverage_Reduction = 253 / 4,254 = 0.059 (5.9%)`
- `Hours_leverage = 4,254 × (1 - 0.059) = 4,003 hours`

**Step 7 — AI Development Effectiveness:**
- AI Maturity Level 3 × High effectiveness (frontend TypeScript): 20% reduction on frontend
- AI Maturity Level 3 × Medium-High effectiveness (backend Java): 12% reduction on backend
- AI Maturity Level 3 × Medium-High effectiveness (Terraform): 10% reduction on infra
- Weighted: (0.40 × 0.20) + (0.45 × 0.12) + (0.15 × 0.10) = 0.08 + 0.054 + 0.015 = **0.149 (14.9%)**
- `Hours_AI_adjusted = 4,003 × (1 - 0.149) = 3,407 hours`

**Step 8 — PERT Three-Point:**
- Optimistic (O): 2,680 hrs (favorable scale factors, maximum leverage, high AI reduction)
- Most Likely (M): 3,407 hrs
- Pessimistic (P): 4,520 hrs (unfavorable scale factors, lower team capability, integration surprises)
- `Expected = (2,680 + 4×3,407 + 4,520) / 6 = 3,471 hours`
- `σ = (4,520 - 2,680) / 6 = 307 hours`

**Manual-Only Estimate (before AI reduction from Step 7):**

Removing the AI effectiveness adjustment (Step 7): `Hours_manual = 4,003 hours` (the leverage-adjusted, stack-multiplied COCOMO II result).

PERT on manual: O = 3,150 hrs, M = 4,003 hrs, P = 5,280 hrs → `Expected = 4,072 hrs`

**Dual Output — Role Breakdown and AI Impact:**

Role percentages for Development phase (twin-adjusted — Dev skews heavily technical): Sr. Product 15%, Jr. Product 10%, Sr. Technical 40%, Jr. Technical 35%.

| Role | Manual Hours (Mid) | AI Effectiveness | Maturity Cap (L3=30%) | Effective Reduction | AI-Assisted Hours |
|---|---|---|---|---|---|
| Sr. Product Eng. | 601 hrs | Very High (47.5%) | 30% | **30%** | 421 hrs |
| Jr. Product Eng. | 400 hrs | High (32.5%) | 30% | **30%** | 280 hrs |
| Sr. Technical Eng. | 1,601 hrs | Very High (47.5%) | 30% | **30%** | 1,121 hrs |
| Jr. Technical Eng. | 1,401 hrs | Medium (20%) | 30% | **20%** | 1,121 hrs |
| **Total** | **4,003 hrs** | | | | **2,943 hrs** |

**AI Delta:** 1,060 hours saved (26.5% reduction). At blended rates: ~$222,600 saved. Development is where AI creates the largest absolute impact — and senior technical engineers represent the single biggest savings line item (480 hrs saved × $250/hr = $120,000). This is because seniors at "Very High" effectiveness hit the maturity ceiling of 30% on a large hour base.

**Note on maturity ceiling impact:** At AI Maturity Level 3, the 30% ceiling constrains "Very High" effectiveness roles from reaching their full 47.5% potential. Moving to Level 4 (ceiling = 45%) would save an additional ~600 hours on this project, worth ~$130,000. This provides a concrete business case for AI maturity investment.

**Final Dual Output:**

| Scenario | Low | Mid | High |
|---|---|---|---|
| **Manual-Only** | 3,150 hrs | 4,003 hrs | 5,280 hrs |
| **AI-Assisted** | 2,316 hrs | 2,943 hrs | 3,884 hrs |
| **AI Delta** | 834 hrs saved | 1,060 hrs saved | 1,396 hrs saved |

**A2A Signal emitted to downstream twins:**
- `languageBreakdown: {"TypeScript": "60%", "Java": "35%", "HCL": "5%"}`
- `expectedPRsPerWeek: 22`
- `averagePRComplexity: "medium"`
- `aiToHumanRatio: {overall: 0.15, byLayer: {frontend: 0.20, backend: 0.12, infrastructure: 0.10}}`
- `infrastructureLeverage: {reusable: ["authentication", "cicd-pipeline", "monitoring"], mustBuild: ["api-gateway", "feature-flags"], leverageScore: 0.59}`

#### 3.3.4 Inference Rules

**Tech stack inference (when not specified):**
- Client in financial services/healthcare + enterprise size → likely Java/Spring or .NET, assume Enterprise Java profile unless stated otherwise
- Client is a startup or technology company → likely JS/TS ecosystem, assume Modern Web (JS/TS) profile
- Mobile app mentioned → check if cross-platform or native is more likely based on client size and budget signals. Startups/SMBs → cross-platform (React Native or Flutter). Enterprise → native per-platform
- "Legacy system" or system age >10 years mentioned → classify using language/framework clues. JSP/Struts → Legacy Web. COBOL/mainframe → Legacy Enterprise. PHP 5.x → Legacy Web. If no clues, default to Legacy Web (1.5-2.5x)
- Data pipeline, ETL, or ML model mentioned → add Data/ML Platform layer to the stack
- Infrastructure-as-code, Kubernetes, or cloud setup mentioned → add Infrastructure/Platform layer

**Infrastructure leverage inference (when not specified):**
- Enterprise clients (5000+ employees) → assume most infrastructure elements exist at "Adaptable" level unless stated otherwise (auth, CI/CD, monitoring, databases are standard in enterprise environments)
- SMB clients (50-500) → assume authentication and basic CI/CD exist, but observability, feature flags, and shared libraries are likely absent
- Startup clients (<50) → assume minimal existing infrastructure, most elements at "Absent" unless explicitly mentioned
- "Greenfield" project type → does NOT mean no infrastructure leverage. The client may have an established platform that the greenfield project deploys onto. Only assume "Absent" across the board if the client is a startup AND it's a greenfield project
- "Existing codebase" toggle (Stage 2, Field 23) set to Yes → infer that at minimum, the database, CI/CD, and testing infrastructure exist at "Reusable" or "Adaptable" level

**Stack-specific AI effectiveness adjustment:**
- Modern Web (JS/TS) or Python + AI maturity Level 3+ → AI-to-human ratio can reach 50-70% for CRUD features, 30-40% for complex business logic
- Enterprise Java/.NET + AI maturity Level 3+ → AI-to-human ratio tops out at 40-50% for CRUD, 20-30% for complex logic (more boilerplate helps AI, but enterprise patterns are less represented in training data)
- Legacy Web + any AI maturity → AI-to-human ratio caps at 10-20% regardless of tooling — the training data simply isn't there
- Legacy Enterprise → AI-to-human ratio effectively 0% — flag that AI tooling provides no meaningful benefit for this stack
- Mobile Native → AI-to-human ratio 30-40% for UI components, 15-25% for platform-specific logic (camera, sensors, permissions)

**Cross-stack integration rules:**
- Frontend + Backend as separate stacks → add API contract definition effort (8-16 hrs for REST, 16-24 hrs for GraphQL schema design) plus ongoing contract maintenance
- Multiple backend services (microservices) → add per-service communication overhead (service discovery, circuit breakers, distributed tracing) at 8-16 hrs per service boundary
- Monolith-to-microservices migration → add decomposition analysis effort (24-60 hrs depending on monolith size) plus the strangler fig overhead per extracted service

#### 3.3.5 Output

- Effort range in hours (low/mid/high), split into:
  - **Infrastructure setup / leverage:** hours for standing up or configuring each infrastructure element, with a line-item breakdown showing "reuse" vs. "build new" per element
  - **Per-layer build effort:** frontend, backend, data layer, infrastructure-as-code — each with its stack-specific multiplier applied
  - **Integration effort:** per-integration line items with type classification and effort
  - **Legacy/migration overhead:** if applicable, broken out from new-build effort
- Tech stack classification used, with the multiplier applied and justification
- Infrastructure leverage scorecard — which elements were scored as Reusable/Adaptable/Incompatible/Absent and how much effort was saved versus a "build everything" baseline
- Expected code output volume (to feed Code Review Sentinel via A2A cross-phase signal) — broken down by language/framework since review effort varies by stack
- AI-to-human ratio per stack layer with justification (e.g., "Frontend React: 55% AI / 45% human at maturity Level 3; Backend Spring Boot: 35% AI / 65% human")
- Risk flags: stack-specific risks (e.g., "if the legacy COBOL system has undocumented business rules, discovery and development effort could increase by 40-60%"), infrastructure leverage risks (e.g., "existing CI/CD pipeline is Jenkins 1.x — adapting it may cost more than building a new GitHub Actions pipeline"), and cross-stack integration risks
- Dependency notes on discovery completeness and design artifact quality

#### 3.3.6 A2A Cross-Phase Signal Schema (Emitted)

The Development Architect emits a richer signal than originally specified, reflecting the stack-aware assessment:

```json
{
  "signalType": "development-assessment",
  "sourceAgent": "development-architect",
  "targetAgents": ["code-review-sentinel", "deployment-devops-engineer", "qa-testing-strategist"],
  "payload": {
    "techStackClassification": {
      "frontend": {"category": "modern-web-js", "framework": "Next.js", "multiplier": 1.0},
      "backend": {"category": "enterprise-java", "framework": "Spring Boot 3", "multiplier": 1.2},
      "infrastructure": {"category": "infrastructure-platform", "tools": ["Terraform", "Kubernetes"], "multiplier": 1.1}
    },
    "codeVolumeEstimate": {
      "expectedPRsPerWeek": 25,
      "languageBreakdown": {"TypeScript": "60%", "Java": "35%", "HCL": "5%"},
      "averagePRComplexity": "medium"
    },
    "aiToHumanRatio": {
      "overall": 0.38,
      "byLayer": {"frontend": 0.55, "backend": 0.35, "infrastructure": 0.20}
    },
    "integrationComplexity": "high",
    "integrationCount": 4,
    "infrastructureLeverage": {
      "reusable": ["authentication", "cicd-pipeline", "monitoring"],
      "mustBuild": ["api-gateway", "feature-flags"],
      "leverageScore": 0.65
    },
    "legacyModernizationInvolved": true,
    "modernizationPattern": "strangler-fig"
  }
}
```

This signal enables downstream twins to calibrate precisely:
- **Code Review Sentinel** uses `languageBreakdown` and `averagePRComplexity` to estimate review effort per language (Java PRs take longer to review than TypeScript PRs) and `aiToHumanRatio.byLayer` to anticipate which code layers will have higher AI-generated content (and thus different review patterns)
- **Deployment & DevOps** uses `techStackClassification.infrastructure` and `infrastructureLeverage` to assess how much pipeline and deployment work is already covered versus needs building, and `modernizationPattern` to plan deployment strategy (strangler fig requires parallel routing infrastructure)
- **QA/Testing Strategist** uses `integrationCount`, `legacyModernizationInvolved`, and `languageBreakdown` to scope integration testing, regression testing (if legacy migration), and framework-specific test tooling setup

### 3.4 Code Review Sentinel

#### 3.4.1 Evaluation Dimensions

- Expected PR volume (derived from Development Architect's output)
- Kickback rate assumptions (percentage of PRs requiring revision or phase-2/3 return)
- Dependency-aware review complexity (cross-project or cross-module review needs)
- Intelligence loop maturity (is there an existing pattern library, or does it need to be established)
- Review tooling setup (AI-assisted review tools, linting, static analysis at the PR level)
- Human review bottleneck risk (ratio of code generation speed to review capacity)

#### 3.4.2 Formal Estimation Algorithm: Fagan Inspection Rate Model with Defect Density Calibration

The Code Review Sentinel uses an adaptation of the **Fagan Inspection** effort model (Fagan, 1976) — the most empirically validated approach to code inspection effort estimation — combined with **Defect Removal Efficiency (DRE)** projections to determine optimal review intensity.

**Step 1 — Code Volume Intake (from Development Architect A2A Signal):**

The twin receives the `codeVolumeEstimate` from the Development Architect's cross-phase signal:
- `expectedPRsPerWeek`: weekly PR volume
- `languageBreakdown`: percentage per language
- `averagePRComplexity`: simple/medium/complex
- Total estimated KSLOC (from COCOMO II sizing)

**Step 2 — Optimal Inspection Rate by Language:**

Research establishes optimal code inspection rates for effective defect detection. Exceeding these rates significantly reduces defect-finding effectiveness:

| Language Category | Optimal Review Rate (LOC/hour) | Preparation Rate (LOC/hour) | Rationale |
|---|---|---|---|
| Java / C# / Go | 150–200 | 200–300 | Verbose, type-safe — more LOC but clearer intent |
| TypeScript / JavaScript | 175–250 | 250–350 | Less verbose, but dynamic typing requires closer scrutiny of edge cases |
| Python | 150–200 | 200–300 | Concise but dynamic typing + whitespace sensitivity |
| C/C++ / Rust | 100–150 | 150–200 | Memory management, pointer logic, safety-critical patterns |
| HCL / YAML (IaC) | 200–300 | 300–400 | Declarative, but blast radius of errors is high — review focuses on correctness not complexity |
| COBOL / Legacy | 75–125 | 100–175 | Limited tooling, unfamiliar patterns, high risk |

**Step 3 — Per-Language Review Hours:**

For each language in the breakdown:

`Review_Hours_lang = (KSLOC_lang × 1000) / Inspection_Rate_lang`

`Preparation_Hours_lang = (KSLOC_lang × 1000) / Preparation_Rate_lang`

`Total_Review_Hours = Σ (Review_Hours_lang + Preparation_Hours_lang)`

**Step 4 — PR Complexity Adjustment:**

Not all PRs are equal. The twin adjusts based on average PR complexity from the Development Architect:

| PR Complexity | Lines Changed (avg) | Review Multiplier | Rationale |
|---|---|---|---|
| Simple | < 100 lines | 0.8 | Quick review, low cognitive load |
| Medium | 100–400 lines | 1.0 | Standard review effort |
| Complex | 400+ lines | 1.4 | Requires multiple passes, architectural review |

Research indicates review effectiveness drops significantly above 400 LOC per session (diminishing defect detection rate).

**Step 5 — Kickback Rate and Rework Multiplier:**

The twin estimates the kickback rate (% of PRs requiring revision) based on:

| Factor | Kickback Rate |
|---|---|
| Mature team, established patterns, strong linting | 10–15% |
| Mixed team, some established patterns | 20–30% |
| New team, no established patterns | 30–45% |
| High AI-generated code ratio (> 50%) | +10–15% additional (AI code requires different review patterns — hallucinated logic, subtle errors) |

Each kickback adds a re-review cycle: `Rework_Multiplier = 1 + (kickback_rate × 0.5)` (re-reviews are faster than initial reviews, ~50% of the effort).

**Step 6 — Defect Density Projection and DRE Target:**

The twin projects expected defect density and calculates the review intensity needed to achieve a target DRE:

`Expected_Defect_Density = Base_DD × Complexity_Factor × (1 - AI_Quality_Adjustment)`

Where:
- `Base_DD` = 5–15 defects per KSLOC (industry benchmark; twin selects based on project domain)
- `Complexity_Factor` = derived from COCOMO II CPLX rating (0.8 for low complexity, 1.0 for nominal, 1.3 for high, 1.7 for very high)
- `AI_Quality_Adjustment` = 0.0–0.15 (AI-generated code that passes linting may have fewer syntactic defects but more semantic defects — net effect is modest)

Target DRE for code review phase: **50–65%** of remaining defects (Capers Jones benchmark: each review/inspection phase removes ~30% of present defects; two-pass review achieves ~50–65%).

If `Expected_Defect_Density × (1 - DRE_target)` exceeds the quality threshold (e.g., > 3 defects/KSLOC escaping to QA), the twin recommends increasing review intensity (slower inspection rate, more preparation time).

**Step 7 — Tooling Setup Effort:**

| Review Tooling Component | If Exists | If Must Set Up |
|---|---|---|
| Automated linting / static analysis (ESLint, SonarQube, etc.) | 2–4 hrs config | 8–16 hrs |
| AI-assisted review (GitHub Copilot for PRs, CodeRabbit, etc.) | 2–4 hrs config | 8–16 hrs |
| PR template and review checklist | 1–2 hrs | 4–8 hrs |
| Pattern library / architectural decision records | 0 hrs | 16–32 hrs |

**Step 8 — Total Code Review Effort:**

```
Review_Effort = (Total_Review_Hours × PR_Complexity_Multiplier × Rework_Multiplier) + Tooling_Setup
```

Apply three-point estimation (PERT) across optimistic/most-likely/pessimistic kickback rates and complexity assumptions.

#### 3.4.3 Worked Example: Healthcare Patient Portal

**Scenario (continued):** Same portal. Development Architect A2A signal received: 8.685 KSLOC total (TypeScript 60% = 5,076 LOC, Java 35% = 3,339 LOC, HCL 5% = 270 LOC), 22 PRs/week expected, average PR complexity = medium, AI-to-human ratio overall = 0.15.

**Step 1 — Code Volume Intake:**
- Total KSLOC: 8.685 (5.076 TypeScript + 3.339 Java + 0.270 HCL)
- Expected PRs/week: 22
- Average PR complexity: medium

**Step 2 — Optimal Inspection Rates:**

| Language | KSLOC | Inspection Rate (LOC/hr) | Preparation Rate (LOC/hr) |
|---|---|---|---|
| TypeScript | 5.076 | 200 | 300 |
| Java | 3.339 | 175 | 250 |
| HCL | 0.270 | 250 | 350 |

**Step 3 — Per-Language Review Hours:**

| Language | Review Hours | Preparation Hours | Total |
|---|---|---|---|
| TypeScript | 5,076 / 200 = 25.4 hrs | 5,076 / 300 = 16.9 hrs | **42.3 hrs** |
| Java | 3,339 / 175 = 19.1 hrs | 3,339 / 250 = 13.4 hrs | **32.5 hrs** |
| HCL | 270 / 250 = 1.1 hrs | 270 / 350 = 0.8 hrs | **1.9 hrs** |
| **Total** | | | **76.7 hrs** |

**Step 4 — PR Complexity Adjustment:**
- Average PR complexity = medium → multiplier = **1.0** (no adjustment)
- `Complexity_Adjusted = 76.7 × 1.0 = 76.7 hours`

**Step 5 — Kickback Rate and Rework:**
- Mixed team, some established patterns (enterprise client) → base kickback rate = **25%**
- AI-generated code ratio ~15% → add 5% → effective kickback rate = **30%**
- `Rework_Multiplier = 1 + (0.30 × 0.5) = 1.15`
- `Review_with_Rework = 76.7 × 1.15 = 88.2 hours`

**Step 6 — Defect Density Projection:**
- Base DD = 8 defects/KSLOC (healthcare domain, moderate complexity)
- Complexity Factor = 1.17 (from COCOMO II CPLX = High)
- AI Quality Adjustment = 0.05 (modest net improvement)
- `Expected_DD = 8 × 1.17 × (1 - 0.05) = 8.89 defects/KSLOC`
- Total expected defects: 8.89 × 8.685 = **77 defects**
- Target DRE = 55% → expect to catch ~42 defects in review
- Defects escaping to QA: ~35 defects (within acceptable range for system testing)

**Step 7 — Tooling Setup:**

| Component | Status | Hours |
|---|---|---|
| Linting/static analysis (ESLint + SonarQube) | Existing (client has SonarQube) | 3 hrs (config) |
| AI-assisted review (GitHub Copilot for PRs) | Must set up | 12 hrs |
| PR template + review checklist | Must create (new project) | 6 hrs |
| Pattern library / ADRs | Must create | 20 hrs |
| **Tooling Total** | | **41 hrs** |

**Step 8 — Total Code Review Effort:**
- `Review_Effort = 88.2 + 41 = 129.2 hours`

**PERT Three-Point:**
- Optimistic (O): 99 hrs (lower kickback rate, existing tooling)
- Most Likely (M): 129 hrs
- Pessimistic (P): 179 hrs (higher kickback rate at 40%, more complex PRs, pattern library takes longer)
- `Expected = (99 + 4×129 + 179) / 6 = 132 hours`
- `σ = (179 - 99) / 6 = 13.3 hours`

**Manual-Only Estimate: Low = 99 hrs | Mid = 132 hrs | High = 179 hrs (±13 hrs confidence interval)**

Note: The Code Review algorithm doesn't have a single AI reduction step — AI assistance is embedded in tooling (AI-assisted review tools reduce preparation time) and kickback rate. The manual baseline uses the same core review hours but assumes no AI-assisted review tooling (higher preparation time, +15%) and no AI review in the kickback loop:

`Manual_Review_Hours = 76.7 × 1.15 × 1.15 = 101.4 hrs` + tooling (without AI review: 29 hrs) = **130 hrs**

**Dual Output — Role Breakdown and AI Impact:**

Role percentages for Code Review phase (twin-adjusted — review is senior-heavy, technical-heavy): Sr. Product 10%, Jr. Product 5%, Sr. Technical 55%, Jr. Technical 30%.

| Role | Manual Hours (Mid) | AI Effectiveness | Maturity Cap (L3=30%) | Effective Reduction | AI-Assisted Hours |
|---|---|---|---|---|---|
| Sr. Product Eng. | 13 hrs | High (32.5%) | 30% | **30%** | 9 hrs |
| Jr. Product Eng. | 6 hrs | Low (7.5%) | 30% | **7.5%** | 6 hrs |
| Sr. Technical Eng. | 72 hrs | High (32.5%) | 30% | **30%** | 50 hrs |
| Jr. Technical Eng. | 39 hrs | Low (7.5%) | 30% | **7.5%** | 36 hrs |
| **Total** | **130 hrs** | | | | **101 hrs** |

**AI Delta:** 29 hours saved (22.3% reduction). At blended rates: ~$6,640 saved. Senior technical engineers account for 76% of savings — AI-assisted code review tools are most effective when a senior reviewer uses them to triage low-risk PRs and focus human attention on architectural and security-critical changes. Junior reviewers get minimal AI benefit because they lack the context to validate AI review suggestions.

**Final Dual Output:**

| Scenario | Low | Mid | High |
|---|---|---|---|
| **Manual-Only** | 100 hrs | 130 hrs | 176 hrs |
| **AI-Assisted** | 77 hrs | 101 hrs | 137 hrs |
| **AI Delta** | 23 hrs saved | 29 hrs saved | 39 hrs saved |

#### 3.4.4 Inference Rules

- High AI-assisted code output from Development Architect → scale review estimate proportionally, flag volume risk; increase kickback rate by 10–15% for AI-generated layers
- New codebase with no established patterns → higher kickback rate in early sprints (35–45%), tapering to 15–20% over time — twin models this as a weighted average across project duration
- Multiple developers/agents working in parallel → increase dependency-aware review overhead by 15–25% (cross-cutting changes, merge conflicts, architectural consistency)
- No existing intelligence loop → add setup cost for pattern tracking and feedback mechanisms (16–32 hrs)
- Language mix with Legacy stacks → use slower inspection rates, flag that review becomes the critical path bottleneck

#### 3.4.4 Output

- Effort range in hours (low/mid/high), split between tooling setup and ongoing review effort
- Per-language review hour breakdown with inspection rates used
- Estimated kickback rate and its impact on Development timeline
- Projected defect density and DRE target with justification
- Assumptions about review-to-development ratio
- Risk flags (e.g., "if PR volume exceeds X per week, human review becomes the critical path — recommend adding a second reviewer or increasing AI-assisted review coverage")
- Feedback loop notes for the Orchestrator (systemic issues to flag back to Development)

### 3.5 Deployment & DevOps Engineer

#### 3.5.1 Evaluation Dimensions

- CI/CD pipeline maturity (existing pipelines versus greenfield setup)
- Quality gate requirements (static analysis, security scanning, prompt checking, OWASP compliance, AI evals)
- Monitoring and observability stack (existing tools versus new implementation)
- Infrastructure provisioning (cloud setup, environment management, IaC)
- Vendor evaluation and lock-in risk (especially for AI-specific deployment tools)
- Operational handoff (documentation, runbooks, training for client ops team)

#### 3.5.2 Formal Estimation Algorithm: Cloud Migration Points (CMP) with Infrastructure Complexity Model

The Deployment & DevOps phase lacks a single dominant parametric model comparable to COCOMO II for development. The twin uses a composite approach: **Cloud Migration Points (CMP)** (Zhao & Zhou, 2014) for infrastructure provisioning effort, combined with a **Work Breakdown Structure (WBS)**-based bottom-up estimation for CI/CD and operational components.

**Step 1 — Infrastructure Complexity Assessment:**

Score the deployment target on four CMP dimensions:

| CMP Dimension | Weight | Scoring Criteria |
|---|---|---|
| Connection Complexity | 0.25 | Number of service-to-service connections, external integrations, network topology requirements. Simple (1-3 connections) = 1, Moderate (4-8) = 2, Complex (9+, multi-region, VPN/peering) = 3 |
| Code/Config Changes | 0.25 | Extent of IaC, containerization, or configuration changes needed. Minimal (existing containers, cloud-native) = 1, Moderate (needs containerization or IaC refactoring) = 2, Extensive (legacy to cloud, major re-platforming) = 3 |
| Installation & Configuration | 0.30 | Environment setup complexity. Single env, simple deploy = 1, Multi-env (dev/staging/prod) with config management = 2, Multi-region, multi-cloud, or hybrid with compliance constraints = 3 |
| Database/Data Layer | 0.20 | Data migration and persistence layer setup. No migration or managed service = 1, Schema migration with moderate data volume = 2, Complex migration with data transformation, large datasets, or real-time sync = 3 |

`CMP_Score = Σ (weight_i × score_i)` → range: 1.0 (trivial) to 3.0 (highly complex)

**Step 2 — Base Infrastructure Effort from CMP:**

| CMP Score Range | Base Infrastructure Hours | Typical Scenario |
|---|---|---|
| 1.0–1.5 | 40–80 hrs | Cloud-native app, single environment, managed services |
| 1.5–2.0 | 80–160 hrs | Multi-environment, moderate IaC, some migration |
| 2.0–2.5 | 160–320 hrs | Multi-region or hybrid, complex networking, significant IaC |
| 2.5–3.0 | 320–600 hrs | Enterprise-scale, multi-cloud, compliance-heavy, major data migration |

**Step 3 — CI/CD Pipeline Effort (WBS Bottom-Up):**

The twin estimates each pipeline component individually based on whether existing infrastructure can be leveraged (from Development Architect's `infrastructureLeverage` signal):

| Pipeline Component | Greenfield (hrs) | Leverage Existing (hrs) | Notes |
|---|---|---|---|
| Source control & branching strategy | 4–8 | 1–2 | Git workflow, branch protection rules |
| Build automation | 8–16 | 2–4 | Compile, bundle, Docker image build |
| Unit test integration | 4–8 | 1–2 | Test runner in pipeline |
| Integration/E2E test stage | 8–16 | 2–4 | Test environment setup, fixtures |
| Static analysis & linting | 4–8 | 1–2 | SonarQube, ESLint, language-specific |
| Security scanning (SAST/DAST) | 8–16 | 2–4 | OWASP, dependency scanning |
| AI-specific quality gates | 12–24 | N/A | Prompt checking, AI evals, model validation (only if AI components in project) |
| Artifact management | 4–8 | 1–2 | Container registry, artifact storage |
| Environment promotion (dev→staging→prod) | 16–32 | 4–8 | Deployment scripts, approval gates |
| Rollback/canary deployment | 8–24 | 2–8 | Blue-green, canary, or rolling strategy |
| Secrets management | 4–8 | 1–2 | Vault, AWS Secrets Manager, etc. |

`CI_CD_Effort = Σ (component_hours)` based on leverage status from the Development Architect signal.

**Step 4 — Monitoring & Observability Effort:**

| Observability Component | Greenfield (hrs) | Leverage Existing (hrs) |
|---|---|---|
| Application Performance Monitoring (APM) | 16–32 | 4–8 |
| Log aggregation & search | 8–16 | 2–4 |
| Metrics & dashboarding | 12–24 | 4–8 |
| Alerting rules & escalation | 8–16 | 2–4 |
| Distributed tracing (if microservices) | 12–24 | 4–8 |
| Synthetic monitoring / health checks | 4–8 | 1–2 |

**Step 5 — Operational Handoff Effort:**

If the client expects to own operations post-launch:

| Handoff Component | Hours |
|---|---|
| Runbook creation (per environment) | 8–16 |
| Architecture & deployment documentation | 16–32 |
| Operations training sessions | 8–24 |
| Incident response procedure definition | 8–16 |
| On-call setup and escalation paths | 4–8 |

**Step 6 — Regulatory Compliance Overhead:**

| Compliance Framework | Additional Effort Multiplier |
|---|---|
| SOC 2 | 1.15–1.25× on CI/CD and monitoring |
| HIPAA | 1.20–1.35× (audit logging, encryption, access controls) |
| PCI-DSS | 1.25–1.40× (network segmentation, key management, vulnerability scanning) |
| FedRAMP | 1.30–1.50× (boundary controls, continuous monitoring, authorization package) |
| None / minimal | 1.0× |

**Step 7 — Total Deployment/DevOps Effort:**

```
DevOps_Effort = (CMP_Infrastructure_Hours + CI_CD_Effort + Monitoring_Effort + Handoff_Effort) × Compliance_Multiplier
```

Apply three-point estimation (PERT) across optimistic (maximum leverage, no surprises) / most-likely / pessimistic (minimal leverage, scope creep in infrastructure requirements).

**Conservative Bias:** This twin applies a systematic 10–15% upward adjustment to its final estimate because DevOps is empirically the least mature phase for AI-assisted acceleration — infrastructure work involves high-stakes, environment-specific configuration that AI tools handle poorly.

#### 3.5.3 Worked Example: Healthcare Patient Portal

**Scenario (continued):** Same portal. AWS deployment target. Development Architect signal: infrastructure leverage score 0.59, reusable = [authentication (Okta), CI/CD (GitHub Actions), monitoring (Datadog)], must build = [API gateway, feature flags]. HIPAA regulated. Client expects to own operations post-launch. Three environments (dev/staging/prod). No AI-specific deployment components.

**Step 1 — Infrastructure Complexity Assessment (CMP):**

| CMP Dimension | Weight | Score | Rationale |
|---|---|---|---|
| Connection Complexity | 0.25 | 2 | 3 external integrations (EHR, insurance, lab) + internal microservice-like separation (frontend + backend + DB) — moderate |
| Code/Config Changes | 0.25 | 2 | Containerization needed (Docker + K8s), Terraform IaC — moderate work |
| Installation & Configuration | 0.30 | 2 | Three environments (dev/staging/prod), config management, secrets, but single-region AWS |
| Database/Data Layer | 0.20 | 1 | PostgreSQL on AWS RDS, no major data migration (new system), managed service |

- `CMP_Score = (0.25 × 2) + (0.25 × 2) + (0.30 × 2) + (0.20 × 1) = 0.5 + 0.5 + 0.6 + 0.2 = 1.80`
- CMP range 1.5–2.0 → **Base Infrastructure Hours: 120 hours** (mid-range)

**Step 2 — CI/CD Pipeline Effort:**

| Component | Status | Hours |
|---|---|---|
| Source control & branching | Leverage existing (GitHub Actions) | 2 hrs |
| Build automation | Leverage existing (extend) | 3 hrs |
| Unit test integration | Leverage existing | 2 hrs |
| Integration/E2E test stage | Greenfield (new project test suites) | 12 hrs |
| Static analysis & linting | Leverage existing (SonarQube) | 2 hrs |
| Security scanning (SAST/DAST) | Greenfield (HIPAA requires dedicated scanning) | 14 hrs |
| AI-specific quality gates | N/A (no AI model components) | 0 hrs |
| Artifact management | Leverage existing (ECR) | 2 hrs |
| Environment promotion (dev→staging→prod) | Greenfield (project-specific) | 24 hrs |
| Rollback/canary deployment | Greenfield (K8s rolling + canary) | 16 hrs |
| Secrets management | Leverage existing (AWS Secrets Manager) | 2 hrs |
| **CI/CD Total** | | **79 hrs** |

**Step 3 — Monitoring & Observability:**

| Component | Status | Hours |
|---|---|---|
| APM (Datadog) | Leverage existing | 6 hrs |
| Log aggregation | Leverage existing (Datadog Logs) | 3 hrs |
| Metrics & dashboarding | Leverage existing (extend) | 6 hrs |
| Alerting rules | Greenfield (new project thresholds) | 12 hrs |
| Distributed tracing | Greenfield (backend services) | 16 hrs |
| Synthetic monitoring | Greenfield | 6 hrs |
| **Monitoring Total** | | **49 hrs** |

**Step 4 — Operational Handoff:**

| Component | Hours |
|---|---|
| Runbook creation (3 environments) | 14 hrs |
| Architecture & deployment documentation | 24 hrs |
| Operations training sessions (2 sessions) | 12 hrs |
| Incident response procedures | 12 hrs |
| On-call setup | 6 hrs |
| **Handoff Total** | **68 hrs** |

**Step 5 — Compliance Multiplier:**
- HIPAA → **1.25×** on CI/CD and monitoring components
- Applied to: CI/CD (79 hrs) + Monitoring (49 hrs) = 128 hrs × 1.25 = 160 hrs (Δ = +32 hrs)

**Step 6 — Total DevOps Effort (before conservative bias):**
- `DevOps_Effort = 120 (infra) + 160 (CI/CD + monitoring with HIPAA) + 68 (handoff) = 348 hours`

**Step 7 — Conservative Bias:**
- 12% upward adjustment: `348 × 1.12 = 390 hours`

**PERT Three-Point:**
- Optimistic (O): 295 hrs (maximum leverage, smooth provisioning)
- Most Likely (M): 390 hrs
- Pessimistic (P): 512 hrs (K8s complexity, HIPAA audit requirements expand, environment issues)
- `Expected = (295 + 4×390 + 512) / 6 = 394 hours`
- `σ = (512 - 295) / 6 = 36 hours`

**Manual-Only Estimate: Low = 295 hrs | Mid = 394 hrs | High = 512 hrs (±36 hrs confidence interval)**

Note: The DevOps twin already includes a 10–15% conservative bias because this phase is the least AI-mature. The manual baseline IS the primary estimate above. The AI-assisted variant applies role-specific reductions on top of this conservative baseline.

**Dual Output — Role Breakdown and AI Impact:**

Role percentages for Deployment/DevOps phase (twin-adjusted — almost entirely technical): Sr. Product 5%, Jr. Product 0%, Sr. Technical 55%, Jr. Technical 40%.

| Role | Manual Hours (Mid) | AI Effectiveness | Maturity Cap (L3=30%) | Effective Reduction | AI-Assisted Hours |
|---|---|---|---|---|---|
| Sr. Product Eng. | 20 hrs | Medium (20%) | 30% | **20%** | 16 hrs |
| Jr. Product Eng. | 0 hrs | — | — | — | 0 hrs |
| Sr. Technical Eng. | 217 hrs | High (32.5%) | 30% | **30%** | 152 hrs |
| Jr. Technical Eng. | 157 hrs | Medium (20%) | 30% | **20%** | 126 hrs |
| **Total** | **394 hrs** | | | | **294 hrs** |

**AI Delta:** 100 hours saved (25.4% reduction). At blended rates: ~$21,640 saved. Senior technical engineers drive 65% of savings — IaC generation (Terraform modules, Helm charts, K8s manifests) is an area where AI tools help experienced engineers move faster while still requiring senior judgment for security and compliance correctness. Junior technical engineers see moderate benefit from AI-templated pipeline components.

**Final Dual Output:**

| Scenario | Low | Mid | High |
|---|---|---|---|
| **Manual-Only** | 295 hrs | 394 hrs | 512 hrs |
| **AI-Assisted** | 220 hrs | 294 hrs | 382 hrs |
| **AI Delta** | 75 hrs saved | 100 hrs saved | 130 hrs saved |

#### 3.5.4 Inference Rules

- No existing CI/CD → use greenfield hours for all pipeline components; this becomes a major line item
- Regulated industry → apply compliance multiplier from Step 6; more quality gates, more compliance scanning
- AI-generated code in the pipeline → add AI-specific quality gates (12–24 hrs for prompt checking, AI evals)
- Client expects to own operations post-launch → include full handoff effort from Step 5
- Development Architect signals `infrastructureLeverage.leverageScore > 0.6` → use "Leverage Existing" hours for CI/CD and monitoring components
- Multi-cloud or hybrid deployment → push CMP score toward upper range, increase connection complexity
- This phase is the least mature for AI assistance → apply 10–15% conservative bias

#### 3.5.4 Output

- Effort range in hours (low/mid/high), split between infrastructure provisioning, CI/CD pipeline setup, quality gate implementation, monitoring/observability, and operational handoff
- CMP score with dimension breakdown and justification
- Pipeline component breakdown showing greenfield vs. leveraged hours
- Compliance multiplier applied with regulatory framework identified
- Assumptions about client infrastructure maturity
- Risk flags (e.g., "vendor lock-in risk with X approach — consider alternative if client is risk-averse")
- Ongoing operational cost estimate (if the proposal includes post-launch support)
- Dependency notes on upstream code quality, review thoroughness, and infrastructure leverage data from Development Architect

### 3.6 QA & Testing Strategist

The QA & Testing Strategist is unique among the six twins: it produces **three distinct cost plans** rather than a single estimate. The effort, staffing, coverage profile, and long-term cost trajectory differ dramatically depending on the QA approach selected. The twin calculates all three and presents a comparison so stakeholders can make an informed tradeoff decision.

#### 3.6.0 Three QA Cost Plans

| | Plan A: Evaluation Harness | Plan B: Formalized QA Team | Plan C: Hybrid |
|---|---|---|---|
| **What it is** | Automated evaluation infrastructure — AI/LLM eval pipelines (Promptfoo, DeepEval, custom eval suites), CI-integrated test automation, synthetic data generation, self-healing tests, coverage dashboards | Dedicated human QA team — manual testers + automation engineers performing structured testing (functional, regression, exploratory, UAT) | Both combined — eval harness for regression/AI coverage + QA team for exploratory, UAT, and edge case validation |
| **Primary staffing** | Sr. Technical Engineers (build the harness) + Jr. Technical Engineers (maintain + extend evals) | QA Lead + Manual Testers (Jr. Product) + Automation Engineers (Jr. Technical) | Harness team (technical) + QA team (product/technical mix) |
| **Upfront cost** | High (infrastructure build) | Low-Medium (onboarding, process setup) | Highest (both investments) |
| **Ongoing cost** | Low (automated, minimal human intervention) | High (continuous staffing) | Medium (harness reduces manual burden) |
| **Coverage profile** | Excellent regression + AI eval coverage; weak on exploratory + UX validation | Excellent exploratory + UAT + edge cases; manual regression is slow and expensive to maintain | Best overall coverage across all test types |
| **Best for** | AI-heavy products, API-first platforms, teams with strong DevOps culture | Consumer-facing apps, regulated industries requiring documented manual test evidence, teams without automation expertise | Enterprise projects with both AI components and regulatory/compliance requirements |

#### 3.6.1 Evaluation Dimensions (Common to All Plans)

- User workflow coverage (number of distinct end-to-end flows requiring test coverage)
- Testing framework selection and setup (effort varies by framework maturity and team familiarity)
- AI-assisted test generation percentage (how much can be auto-generated versus manually written)
- Human validation irreducible effort (defining "correct," exploratory testing, edge case identification)
- Performance and load testing requirements
- Regression testing scope (especially important for legacy system replacements with data migration)
- UAT/product validation coordination (client involvement, feedback cycle duration)

**Plan-specific dimensions:**

- **Plan A additional:** Eval dataset curation effort, LLM-as-judge calibration, eval metric implementation (reference-based vs. reference-less), CI/CD eval gate integration, synthetic test data generation pipeline, self-healing test maintenance, eval suite coverage dashboard setup
- **Plan B additional:** QA team staffing model (tester-to-developer ratio, manual-to-automation engineer ratio), test case documentation overhead, manual test execution throughput, UAT coordination effort per user role, test evidence documentation for compliance
- **Plan C additional:** All of the above, plus integration effort between eval harness and QA team workflows, overlap elimination (which tests are harness-owned vs. team-owned), unified reporting across automated and manual test results

#### 3.6.2 Formal Estimation Algorithms (Three Plans)

All three plans share Steps 1–3 (Test Point Analysis sizing) but diverge on how the test points are fulfilled.

**Shared Foundation — Test Point Analysis (TPA) Sizing (Steps 1–3):**

The QA twin uses **Test Point Analysis (TPA)** (Sogeti/TMap, Erik van Veenendaal) to size the testing scope independently of which plan fulfills it.

**Step 1 — Dynamic Test Points:**

For each function (derived from the project's Function Point inventory or the screen inventory from the UX twin):

`Dynamic_TP_function = FP_function × Df × Qd`

Where:
- `FP_function` = the function point weight of the function being tested (from IFPUG: EI weight 3/4/6, EO weight 4/5/7, EQ weight 3/4/6, ILF weight 7/10/15, EIF weight 5/7/10)
- `Df` = Dependency Factor:

| Dependency Level | Df Value | Criteria |
|---|---|---|
| Low | 0.5 | Function is standalone, testable in isolation |
| Average | 1.0 | Function has 2–3 dependencies on other functions |
| High | 1.5 | Function has 4+ dependencies, requires complex setup or data orchestration |

- `Qd` = Dynamic Quality Characteristics factor:

`Qd = (0.75 × Qf + 0.05 × Qp + 0.10 × Qu + 0.10 × Qe) / 4`

Where each Q factor is rated 0 (not applicable), 3 (lightly tested), 4 (moderately tested), or 6 (thoroughly tested):

| Factor | What It Measures |
|---|---|
| Qf (Functionality) | Correctness and completeness of business logic |
| Qp (Security) | Authentication, authorization, data protection testing |
| Qu (Usability) | User interface consistency, error handling, accessibility |
| Qe (Efficiency) | Response time, throughput, resource utilization under load |

`Total_Dynamic_TP = Σ (Dynamic_TP_function)` for all functions

**Step 2 — Static Test Points:**

`Static_TP = (Total_FP × Qi) / 500`

Where `Qi` is the static quality rating from 6 ISO 25010 quality characteristics, each assigned 0 or 16 points: `Qi = Σ quality_characteristic_points` → range: 0–96

**Step 3 — Total Test Points:**

`Total_TP = Total_Dynamic_TP + Static_TP`

---

#### 3.6.2A Plan A: Evaluation Harness (Automated Infrastructure)

**What:** Build an automated testing and evaluation infrastructure — CI-integrated test suites, AI/LLM eval pipelines, synthetic data generation, self-healing tests, and coverage dashboards. Minimal ongoing human QA staffing; the harness runs continuously.

**Who builds it:** Primarily Sr. Technical Engineers (architecture + eval pipeline design) and Jr. Technical Engineers (implementation + maintenance). Product engineers may contribute eval dataset curation for domain-specific scenarios.

**Step A1 — Eval Harness Infrastructure Build:**

| Component | Build Effort (hrs) | Maintenance (hrs/month) | Description |
|---|---|---|---|
| **Test automation framework setup** | 40–80 | 4–8 | Playwright/Cypress + Jest/Vitest, CI integration, parallelization |
| **AI/LLM eval pipeline** | 60–120 | 8–16 | Promptfoo/DeepEval/custom eval runner, LLM-as-judge calibration, eval metric implementation (per Anthropic's eval patterns: outcome grading + trajectory analysis) |
| **Eval dataset curation** | 40–80 | 8–16 | Golden test cases, edge case corpus, adversarial inputs, domain-specific scenarios. Reference-based metrics require expected outputs for each test case |
| **Synthetic data generation pipeline** | 24–48 | 4–8 | HIPAA/compliance-safe synthetic data, data virtualization, realistic test fixtures |
| **Self-healing test infrastructure** | 16–32 | 2–4 | Locator auto-repair, visual diff detection, flaky test quarantine (reduces ~2 days/sprint of manual test repair) |
| **Coverage dashboards & reporting** | 16–32 | 2–4 | Test coverage visualization, eval score trending, regression alerts, stakeholder-readable reports |
| **CI/CD eval gates** | 16–24 | 2–4 | Sentinel evals that block deployment on regression, quality gate thresholds, eval-as-CI integration |
| **Guardrail validation suite** | 24–48 | 4–8 | Prompt injection testing, PII detection, hallucination benchmarks, safety eval (40+ attack vectors per DeepEval red-teaming) |
| **LLM-as-judge calibration** | 16–32 | 4–8 | Human annotation for judge calibration (ground truth), inter-annotator agreement measurement, judge prompt iteration |

`Harness_Build_Total = Σ component_build_hours` (range: 252–496 hrs)

**Step A2 — Test Point Coverage via Harness:**

The harness covers test points through automation:

| Test Type | % of Total_TP Covered | Automation Rate | Effective Pf (hrs/TP) |
|---|---|---|---|
| Unit tests | 35% | 90–95% automated | 0.4 |
| Integration tests | 30% | 70–85% automated | 0.6 |
| E2E tests | 20% | 60–75% automated | 0.8 |
| Static analysis | 100% of Static_TP | Fully automated | 0.1 |

`Harness_Test_Hours = Σ (TP_coverage × Pf_harness)` — the ongoing cost of writing and maintaining the automated tests that cover these test points.

**Step A3 — Coverage Gaps (what the harness can't cover):**

| Gap | Manual Effort Required |
|---|---|
| Exploratory testing | 10–15% of Primary_Test_Hours — irreducible human judgment; no harness replaces a skilled tester finding unexpected paths |
| UAT coordination | 2–3 hrs per user role × UAT cycles — humans must validate business correctness |
| Usability/accessibility testing | 8–16 hrs — automated tools catch some WCAG violations but not UX friction |
| Performance testing design | 8–16 hrs — load profiles and SLA thresholds require human definition; execution is automated |

`Gap_Hours = Σ manual_effort_items`

**Step A4 — Plan A Total:**

```
Plan_A_Effort = Harness_Build_Total + Harness_Test_Hours + Gap_Hours
```

**Ongoing monthly cost:** `Σ maintenance_hours` (range: 38–76 hrs/month)

**Plan A Role Distribution:** Sr. Technical 45%, Jr. Technical 40%, Sr. Product 10%, Jr. Product 5%

---

#### 3.6.2B Plan B: Formalized QA Team (Dedicated Testers)

**What:** Staff a dedicated QA team performing structured testing — test planning, manual test case writing and execution, automation scripting, UAT facilitation, and compliance test evidence documentation. Traditional QA model with defined roles and processes.

**Who:** QA Lead (Sr. Product or Sr. Technical), Manual Testers (Jr. Product), Automation Engineers (Jr. Technical). Industry benchmarks suggest a tester-to-developer ratio of 1:3 for standard projects, 1:2 for regulated/safety-critical projects.

**Step B1 — QA Team Staffing Model:**

| Role | Responsibility | Typical Ratio to Dev Team |
|---|---|---|
| QA Lead | Test strategy, test plan, coordination, reporting, risk-based prioritization | 1 per project |
| Sr. Manual Tester | Exploratory testing, UAT facilitation, complex scenario design, compliance documentation | 1 per 4–5 developers |
| Jr. Manual Tester | Test case execution, regression testing, bug documentation, test data preparation | 1 per 3–4 developers |
| Automation Engineer | Framework setup, test script development, CI integration, maintenance | 1 per 5–6 developers |

**Step B2 — Test Point Coverage via QA Team:**

The QA team covers test points through a mix of manual execution and scripted automation:

| Test Type | % of Total_TP Covered | Manual vs. Automated | Pf (hrs/TP) |
|---|---|---|---|
| Unit tests | 35% | Mostly automated by developers (not QA team scope) | 0.5 (QA validates coverage) |
| Integration tests | 30% | 50% manual / 50% scripted | 1.2 |
| E2E functional tests | 25% | 70% manual / 30% scripted | 1.5 |
| Exploratory testing | 10% | 100% manual | 2.0 |
| Static quality review | 100% of Static_TP | Manual review + tool-assisted | 0.5 |

`Team_Test_Hours = Σ (TP_coverage × Pf_team)` — significantly higher than Plan A due to manual execution overhead.

**Step B3 — QA Process Overhead:**

| Activity | Hours | Rationale |
|---|---|---|
| Test strategy & plan documentation | 24–40 | Required for formalized QA; defines scope, approach, entry/exit criteria |
| Test case writing & documentation | 1.5–2 hrs per test case × estimated test case count | Formal test cases with preconditions, steps, expected results |
| Test environment management | 8–16/sprint | Shared environments require coordination, data refresh, conflict resolution |
| Defect triage & reporting | 4–8/sprint | Bug meetings, severity classification, regression verification |
| Test evidence packaging (compliance) | 16–40 | If regulated: signed test results, traceability matrix, compliance artifacts |
| UAT facilitation & support | 3–4 hrs per user role × UAT cycles | Hands-on support for non-technical stakeholders during acceptance testing |
| QA team onboarding & ramp-up | 16–24 per new QA team member | Domain training, tool familiarization, process orientation |

`Process_Overhead = Σ process_hours`

**Step B4 — Plan B Total:**

```
Plan_B_Effort = Team_Test_Hours + Process_Overhead + Automation_Framework_Setup
```

Where `Automation_Framework_Setup` = 24–60 hrs (less than Plan A because the team builds targeted automation, not a comprehensive harness)

**Ongoing monthly cost:** QA team staffing is continuous. Industry benchmark: 1 QA lead + 2 manual testers + 1 automation engineer ≈ $8,000–$12,000/month for staff augmentation, or at loaded hourly rates from the project rate table.

**Plan B Role Distribution:** Sr. Product 20%, Jr. Product 35%, Sr. Technical 15%, Jr. Technical 30%

---

#### 3.6.2C Plan C: Hybrid (Eval Harness + QA Team)

**What:** Build the evaluation harness infrastructure (Plan A) AND staff a formalized QA team (Plan B), with clearly defined ownership boundaries. The harness handles regression, AI evals, and automated coverage; the QA team handles exploratory testing, UAT, compliance evidence, and edge case validation.

**Step C1 — Ownership Boundary Definition:**

| Test Domain | Owner | Rationale |
|---|---|---|
| Unit tests | Harness (automated) | No manual involvement needed |
| Integration tests | Harness (70%) + QA Team (30%) | Harness covers API contracts and data flows; QA team tests complex cross-service scenarios |
| E2E functional tests | Harness (50%) + QA Team (50%) | Harness covers happy paths and regression; QA team tests edge cases and error flows |
| AI/LLM eval | Harness (100%) | Eval pipeline, guardrails, LLM-as-judge — fully automated |
| Exploratory testing | QA Team (100%) | Irreducible human judgment |
| UAT | QA Team (100%) | Stakeholder-facing, requires facilitation |
| Performance testing | Harness (execution) + QA Team (design) | Automated execution, human-designed load profiles |
| Security testing | Harness (automated scanning) + QA Team (penetration testing) | Two-layer security coverage |
| Compliance evidence | QA Team (100%) | Requires signed documentation, traceability |

**Step C2 — Effort Calculation:**

The hybrid is NOT simply Plan A + Plan B — there is significant overlap elimination:

```
Plan_C_Effort = Plan_A_Harness_Build × 1.0          # Full harness build (no reduction)
              + Plan_A_Harness_Test_Hours × 0.7       # Harness covers fewer test points (QA team handles some)
              + Plan_B_Team_Test_Hours × 0.5           # QA team scope is reduced (harness covers regression + AI evals)
              + Plan_B_Process_Overhead × 0.8          # Process overhead partially reduced (harness provides automated reporting)
              + Integration_Effort                     # Effort to integrate harness + team workflows
```

`Integration_Effort = 24–40 hrs` (unified reporting dashboard, test ownership matrix, handoff procedures between harness and team, shared test data management)

**Step C3 — Coverage Advantage:**

The hybrid achieves the highest test coverage because it eliminates both plans' weaknesses:
- Plan A's gap (exploratory, UAT, compliance evidence) is covered by the QA team
- Plan B's gap (scalable regression, AI eval, continuous monitoring) is covered by the harness
- Expected Defect Removal Efficiency: 80–90% (vs. Plan A's 65–75% and Plan B's 70–80%)

**Plan C Role Distribution:** Sr. Product 15%, Jr. Product 20%, Sr. Technical 35%, Jr. Technical 30%

---

#### 3.6.2D Common Steps (All Plans)

**AI-Assisted Test Generation Adjustment:**

Applied to whichever plan is selected, reducing effort on test creation (not infrastructure build):

| AI Maturity Level | Test Type | AI Reduction |
|---|---|---|
| Level 4–5 | Unit tests | 40–60% |
| Level 4–5 | Integration tests | 20–30% |
| Level 4–5 | E2E tests | 10–20% |
| Level 3 | Unit tests | 20–35% |
| Level 3 | Integration/E2E tests | 5–15% |
| Level 1–2 | Any | 0% |

AI-generated tests require validation effort — add 15–25% of AI-generated test hours back as human review.

**Supplementary Testing (additive to all plans):**

| Testing Activity | Estimation Method | Hours |
|---|---|---|
| Performance/Load testing | 1.5–3 hrs per critical user flow × number of flows | Variable |
| Security/Penetration testing | 24–60 hrs (specialist estimate) | Variable |
| Regression testing (legacy replacement) | 30–50% of base test hours if legacy modernization involved | Variable |
| Data migration validation | 2–4 hrs per data entity × migrated entities | Variable |

**Cross-Check — Capers Jones Ratio:**

The twin compares each plan's QA effort against the Capers Jones benchmark (QA/testing = 30–40% of total project effort). Plan A typically comes in below this range (justified by automation efficiency), Plan B within it, and Plan C at or slightly above it (justified by comprehensive coverage). Deviations exceeding ±10 percentage points are flagged.

#### 3.6.3 Worked Example: Healthcare Patient Portal (Three-Plan Comparison)

**Scenario (continued):** Same portal. 180 FP total. AI maturity Level 3. Code Review Sentinel projects 8.89 defects/KSLOC with 55% DRE (35 defects escaping to QA). Development Architect signals 3 integrations (EHR, insurance, lab), no legacy modernization. Deployment twin reports stable but new test environments. Client has limited QA staff (UAT coordination will need support). The system includes AI/LLM features (patient-facing chatbot, clinical note summarizer) requiring specialized evaluation.

##### Shared TPA Sizing (Steps 1–3)

**Step 1 — Dynamic Test Points:**

Representative function sample (from 180 FP, showing key functions):

| Function | FP Weight | Df | Qd | Dynamic TP |
|---|---|---|---|---|
| Patient Login (EI, simple) | 3 | 0.5 | 0.56 | 0.84 |
| Appointment Booking (EI, complex) | 6 | 1.5 | 0.75 | 6.75 |
| Patient Record View (EQ, average) | 4 | 1.0 | 0.56 | 2.24 |
| Patient Record Edit (EI, average) | 4 | 1.5 | 0.75 | 4.50 |
| Lab Results Display (EO, average) | 5 | 1.0 | 0.56 | 2.80 |
| Secure Messaging (EI, complex) | 6 | 1.5 | 0.75 | 6.75 |
| Insurance Claim Submit (EO, complex) | 7 | 1.5 | 0.75 | 7.88 |
| Billing Summary (EQ, average) | 4 | 1.0 | 0.56 | 2.24 |
| Patient Dashboard (EO, complex) | 7 | 1.5 | 0.56 | 5.88 |
| Video Consultation (EI, complex) | 6 | 1.5 | 0.94 | 8.46 |
| *... remaining 170 FP worth* | *~avg 4.5* | *~avg 1.0* | *~avg 0.65* | *~498* |

- `Total_Dynamic_TP = 48.3 (shown) + 498 (remaining) ≈ 546 test points`

**Step 2 — Static Test Points:**

| Quality Characteristic | Required? | Points |
|---|---|---|
| Maintainability | Yes (enterprise handoff) | 16 |
| Portability | No | 0 |
| Reliability | Yes (HIPAA, healthcare) | 16 |
| Security | Yes (HIPAA) | 16 |
| Performance | Yes (patient-facing) | 16 |
| Usability | Yes (patient-facing) | 16 |

- `Qi = 80`
- `Static_TP = (180 × 80) / 500 = 28.8 test points`

**Step 3 — Total Test Points:**
- `Total_TP = 546 + 28.8 = 574.8 ≈ 575 test points`

*All three plans share Steps 1–3 above (575 total test points). Plans diverge at Step 4.*

---

##### Plan A — Evaluation Harness

**Step 4A — Harness Build Effort:**

| Harness Component | Build Hours | Description |
|---|---|---|
| LLM eval pipeline (Promptfoo/DeepEval) | 80 hrs | Eval dataset curation, metric definition (faithfulness, relevance, safety), baseline calibration for chatbot + summarizer |
| Guardrail validation framework | 48 hrs | Input/output guardrails for HIPAA PHI detection, hallucination checks, clinical safety gates |
| Synthetic data generation | 40 hrs | HIPAA-compliant synthetic patient records, edge-case clinical scenarios, adversarial prompts |
| Self-healing test suite | 56 hrs | Auto-updating selectors, flaky test detection, test-impact analysis for CI feedback loops |
| CI/CD eval gates | 32 hrs | Eval pass/fail thresholds integrated into GitHub Actions pipeline, regression detection, deployment gates |
| LLM-as-judge calibration | 24 hrs | Human-judge agreement scoring, inter-rater reliability baselines, judge prompt optimization |
| Traditional automated test framework (Playwright + Jest) | 40 hrs | E2E test harness, component test scaffolding, API contract tests |
| Test data management | 32 hrs | Fixtures, factories, HIPAA-compliant data masking, environment seeding |
| **Harness Build Total** | **352 hrs** | |

**Step 5A — Test Point Coverage via Automation:**

| Coverage Method | TP Covered | Hrs/TP | Hours | Notes |
|---|---|---|---|---|
| Automated functional tests (E2E + API) | 345 TP (60%) | 0.60 | 207 hrs | Lower hrs/TP due to harness leverage |
| LLM eval suite (AI feature coverage) | 58 TP (10%) | 0.45 | 26 hrs | Eval pipeline handles AI-specific test points |
| Guardrail + safety tests | 29 TP (5%) | 0.55 | 16 hrs | Automated guardrail assertions |
| Performance/load automation | 29 TP (5%) | 1.0 | 29 hrs | Scripted load tests via k6/Artillery |
| **Automated Total** | **461 TP (80%)** | | **278 hrs** | |

**Step 6A — Coverage Gaps (Manual Testing Required):**

| Gap Area | TP Remaining | Hrs/TP | Hours | Rationale |
|---|---|---|---|---|
| Exploratory testing | 58 TP (10%) | 1.2 | 70 hrs | Edge cases, usability, clinical workflow validation |
| UAT coordination | 29 TP (5%) | 0.7 | 20 hrs | 3 user roles × 3 cycles, stakeholder sign-off |
| Security/penetration (specialist) | 27 TP (5%) | 0.5 | 13 hrs | HIPAA pen-test beyond automated SAST/DAST |
| **Gap Total** | **114 TP (20%)** | | **103 hrs** | |

**Step 7A — Supplementary Testing:**

| Activity | Hours |
|---|---|
| Performance/Load (manual analysis of automated results) | 8 hrs |
| Security audit review (HIPAA compliance report) | 24 hrs |
| UAT facilitation (beyond TP-based) | 16 hrs |
| Exploratory (clinical edge cases) | 16 hrs |
| **Supplementary Total** | **64 hrs** |

**Plan A Manual Total:** `352 (build) + 278 (automated) + 103 (gaps) + 64 (supplementary) = 797 ≈ 798 hours`

**Step 8A — AI Adjustment (Plan A):**

| Activity | Manual Hours | AI Reduction | Review Overhead | Net Savings |
|---|---|---|---|---|
| Harness build (code generation) | 352 hrs | 25% → -88 hrs | +18 hrs | **-70 hrs** |
| Automated test creation | 278 hrs | 30% → -83 hrs | +17 hrs | **-67 hrs** |
| Gap testing | 103 hrs | 5% → -5 hrs | +1 hr | **-4 hrs** |
| Supplementary | 64 hrs | 15% → -10 hrs | +2 hrs | **-8 hrs** |
| **Total** | **798 hrs** | | | **-149 hrs** |

**Plan A AI-Assisted Total:** `798 - 149 = 649 hrs`

**Plan A PERT:**
- O = 618 hrs, M = 798 hrs, P = 1,037 hrs → `Expected = 810 hrs, σ = 70 hrs`
- AI-assisted: O = 480 hrs, M = 649 hrs, P = 844 hrs → `Expected = 659 hrs`

---

##### Plan B — Formalized QA Team

**Step 4B — Environment & Productivity Factors:**
- New test environments, managed by DevOps twin → Ef = **0.85** (Good)
- Skilled dedicated QA team, moderate tooling, manual-first approach → Pf = **1.0 hrs/TP**

**Step 5B — Primary Test Hours:**
- `Primary_Test_Hours = 575 × 0.85 × 1.0 = 489 hours`

**Step 6B — Staffing Model:**

| QA Role | Count | Focus Area | Hrs/Person |
|---|---|---|---|
| QA Lead / Test Architect | 1 | Strategy, planning, reporting, UAT coordination | 120 hrs |
| Senior QA Engineer | 1 | Complex integration tests, security testing, performance | 160 hrs |
| QA Engineer (mid) | 2 | Functional testing, regression, test case creation | 280 hrs (140 each) |
| Junior QA / Test Analyst | 1 | Smoke testing, data validation, test execution | 96 hrs |
| **Team Total** | **5** | | **656 hrs** |

Note: Team hours (656) exceed raw Primary_Test_Hours (489) because the staffing model includes management overhead, planning, and coordination that TPA doesn't capture directly.

**Step 7B — Process Overhead:**

| Process Activity | Calculation | Hours |
|---|---|---|
| Test planning & strategy | 8% of Primary_Test_Hours | 39 hrs |
| Test case documentation | 1.5 hrs per 10 TP | 86 hrs |
| Defect management & triage | 2 hrs per estimated defect (35 escaping) | 70 hrs |
| Status reporting & meetings | 4 hrs/week × 12 weeks | 48 hrs |
| Test environment management | 2 hrs/week × 12 weeks | 24 hrs |
| Knowledge transfer & onboarding | 5 days × 8 hrs (team ramp-up) | 40 hrs |
| **Process Overhead Total** | | **307 hrs** |

**Step 8B — Minimal Automation:**

| Automation Activity | Hours |
|---|---|
| Smoke test suite (critical paths only) | 24 hrs |
| Basic CI integration (run smoke on deploy) | 16 hrs |
| **Automation Total** | **40 hrs** |

**Step 9B — Supplementary Testing:**

| Activity | Hours |
|---|---|
| Performance/Load testing | 16 hrs |
| Security/Penetration testing (HIPAA) | 48 hrs |
| UAT coordination (3 roles × 3 cycles × 3 hrs) | 27 hrs |
| Exploratory testing (15% of Primary) | 73 hrs |
| **Supplementary Total** | **164 hrs** |

**Plan B Manual Total:** `656 (team) + 307 (process) + 40 (automation) + 164 (supplementary) = 1,167 hours`

**Step 10B — AI Adjustment (Plan B):**

| Activity | Manual Hours | AI Reduction | Review Overhead | Net Savings |
|---|---|---|---|---|
| Team test execution | 656 hrs | 15% → -98 hrs | +20 hrs | **-79 hrs** |
| Process overhead | 307 hrs | 10% → -31 hrs | +6 hrs | **-25 hrs** |
| Automation setup | 40 hrs | 25% → -10 hrs | +2 hrs | **-8 hrs** |
| Supplementary | 164 hrs | 10% → -16 hrs | +3 hrs | **-13 hrs** |
| **Total** | **1,167 hrs** | | | **-125 hrs** |

**Plan B AI-Assisted Total:** `1,167 - 125 = 1,042 hrs`

**Plan B PERT:**
- O = 934 hrs, M = 1,167 hrs, P = 1,517 hrs → `Expected = 1,184 hrs, σ = 97 hrs`
- AI-assisted: O = 782 hrs, M = 1,042 hrs, P = 1,354 hrs → `Expected = 1,058 hrs`

---

##### Plan C — Hybrid (Evaluation Harness + QA Team)

**Step 4C — Ownership Boundary:**

| Testing Domain | Owner | Rationale |
|---|---|---|
| AI/LLM feature evaluation | Harness (automated) | Eval pipeline, guardrails, LLM-as-judge — requires specialized infrastructure |
| Automated functional/regression | Harness (automated) | Self-healing suite, CI/CD gates — better ROI via automation |
| Exploratory / edge-case testing | QA Team (manual) | Human judgment for clinical workflows, usability, complex integrations |
| UAT coordination & sign-off | QA Team (manual) | Stakeholder relationships, domain expertise |
| Security / compliance testing | QA Team (specialist) | HIPAA audit trail, pen-testing requires human expertise |
| Performance testing | Shared | Harness runs automated load scripts; QA team analyzes results |

**Step 5C — Harness Build (Reduced Scope):**

The hybrid harness focuses on AI evals + automated functional coverage, with reduced scope vs. Plan A (QA team handles gaps):

| Harness Component | Build Hours | vs. Plan A |
|---|---|---|
| LLM eval pipeline | 80 hrs | Same |
| Guardrail validation | 48 hrs | Same |
| Synthetic data generation | 32 hrs | Reduced (QA team supplements with manual test data) |
| Self-healing test suite | 40 hrs | Reduced (smaller scope, QA team covers edge cases) |
| CI/CD eval gates | 32 hrs | Same |
| LLM-as-judge calibration | 24 hrs | Same |
| Traditional automated framework | 32 hrs | Reduced (QA team handles complex E2E) |
| Test data management | 24 hrs | Reduced (shared with QA team) |
| **Hybrid Harness Build** | **312 hrs** | -40 hrs vs. Plan A |

**Step 6C — QA Team (Reduced Scope):**

| QA Role | Count | Focus Area | Hrs/Person |
|---|---|---|---|
| QA Lead / Test Architect | 1 | Strategy, coordination between harness & team | 80 hrs |
| Senior QA Engineer | 1 | Exploratory, security, complex integration | 120 hrs |
| QA Engineer (mid) | 1 | Functional gaps, UAT, regression | 120 hrs |
| **Reduced Team Total** | **3** | | **320 hrs** |

**Step 7C — Process Overhead (Reduced):**

| Process Activity | Hours | vs. Plan B |
|---|---|---|
| Test planning (harness + manual coordination) | 32 hrs | Reduced (harness auto-reports) |
| Test case documentation | 40 hrs | Reduced (harness generates coverage reports) |
| Defect management | 50 hrs | Reduced (harness auto-triages AI defects) |
| Status reporting | 32 hrs | Reduced (dashboard from harness) |
| Knowledge transfer | 24 hrs | Reduced (smaller team) |
| **Process Overhead Total** | **178 hrs** | -129 hrs vs. Plan B |

**Step 8C — Overlap-Adjusted Effort:**

`Hybrid_Raw = Harness_Build + Harness_Test_Hours + QA_Team_Hours + Process_Overhead`
`Hybrid_Raw = 312 + 230 + 320 + 178 = 1,040 hrs`

Overlap factor: some test points are covered by both harness and team (intentional redundancy for HIPAA-critical paths):
- Overlap TPs: ~58 TP (10% of total) tested by both → `Overlap_Hours = 58 × 0.7 = 41 hrs`
- `Hybrid_Adjusted = 1,040 + 41 = 1,081 hrs` (overlap ADDS hours — it's intentional dual-coverage, not waste)

**Step 9C — Supplementary Testing:**

| Activity | Hours | Owner |
|---|---|---|
| Performance/Load (analysis) | 12 hrs | Shared |
| Security/Penetration (HIPAA) | 40 hrs | QA Team specialist |
| UAT coordination | 20 hrs | QA Team |
| Exploratory (clinical edge cases) | 24 hrs | QA Team |
| **Supplementary Total** | **96 hrs** | |

**Plan C Manual Total:** `1,081 (overlap-adjusted) + 96 (supplementary) = 1,177 hours`

**Step 10C — AI Adjustment (Plan C):**

| Activity | Manual Hours | AI Reduction | Review Overhead | Net Savings |
|---|---|---|---|---|
| Harness build | 312 hrs | 25% → -78 hrs | +16 hrs | **-62 hrs** |
| Harness test execution | 230 hrs | 30% → -69 hrs | +14 hrs | **-55 hrs** |
| QA team testing | 320 hrs | 15% → -48 hrs | +10 hrs | **-38 hrs** |
| Process overhead | 178 hrs | 10% → -18 hrs | +4 hrs | **-14 hrs** |
| Supplementary | 96 hrs | 10% → -10 hrs | +2 hrs | **-8 hrs** |
| **Total** | **1,177 hrs** | | | **-177 hrs** |

**Plan C AI-Assisted Total:** `1,177 - 177 = 1,000 hrs`

**Plan C PERT:**
- O = 942 hrs, M = 1,177 hrs, P = 1,530 hrs → `Expected = 1,196 hrs, σ = 98 hrs`
- AI-assisted: O = 750 hrs, M = 1,000 hrs, P = 1,300 hrs → `Expected = 1,017 hrs`

---

##### Three-Plan Comparison

| Dimension | Plan A (Eval Harness) | Plan B (QA Team) | Plan C (Hybrid) |
|---|---|---|---|
| **Manual Mid Estimate** | 798 hrs | 1,167 hrs | 1,177 hrs |
| **AI-Assisted Mid** | 649 hrs | 1,042 hrs | 1,000 hrs |
| **AI Delta** | 149 hrs (18.7%) | 125 hrs (10.7%) | 177 hrs (15.0%) |
| **Upfront Investment** | High (352 hrs harness) | Low (40 hrs automation) | High (312 hrs harness) |
| **Ongoing Cost (monthly)** | Low (~$8K maintenance) | High (~$36K team salaries) | Medium (~$22K combined) |
| **12-Month Total Cost** | ~$208K | ~$432K | ~$362K |
| **DRE (Defect Removal Eff.)** | 70–80% (automated consistency) | 55–65% (human judgment) | 75–85% (best of both) |
| **AI Feature Coverage** | Excellent (native eval pipeline) | Poor (manual prompt testing) | Excellent (eval pipeline) |
| **HIPAA Compliance** | Good (automated guardrails) | Good (human audit trail) | Best (automated + human) |
| **Scalability** | Excellent (marginal cost near zero) | Poor (linear headcount) | Good (harness scales, team stable) |
| **Best Fit** | AI-heavy products, small teams | Traditional apps, regulatory-heavy | Healthcare + AI (this project) |

**Recommendation for this scenario:** **Plan C (Hybrid)** is the best fit for the healthcare patient portal because:
1. AI features (chatbot, summarizer) require the eval harness infrastructure — Plan B cannot adequately test these
2. HIPAA compliance benefits from human QA expertise for audit trails and security testing — Plan A alone lacks this
3. Intentional dual-coverage on critical clinical paths provides the highest DRE (75–85%), justified for healthcare
4. 12-month cost ($362K) is between Plan A and Plan B, with the best risk-adjusted coverage profile

---

##### Dual Output — Role Breakdown and AI Impact (Using Plan C)

Role percentages for QA/Testing phase: Sr. Product 15%, Jr. Product 20%, Sr. Technical 30%, Jr. Technical 35%.

| Role | Plan C Manual Hours | AI Effectiveness | Maturity Cap (L3=30%) | Effective Reduction | AI-Assisted Hours |
|---|---|---|---|---|---|
| Sr. Product Eng. | 177 hrs | Medium (20%) | 30% | **20%** | 142 hrs |
| Jr. Product Eng. | 235 hrs | High (32.5%) | 30% | **30%** | 165 hrs |
| Sr. Technical Eng. | 353 hrs | Medium (20%) | 30% | **20%** | 282 hrs |
| Jr. Technical Eng. | 412 hrs | High (32.5%) | 30% | **30%** | 288 hrs |
| **Total** | **1,177 hrs** | | | | **877 hrs** |

Note: Summed AI-assisted hours (877) differ slightly from the component-based calculation (1,000 hrs) due to rounding at the role level vs. activity level. The component-based figure (1,000 hrs) is the primary estimate; the role breakdown is indicative for cost attribution.

**AI Delta:** 177 hours saved (15.0% reduction). At blended rates: ~$29,736 saved. QA remains unique among twins: **junior roles benefit MORE from AI than seniors** — AI-generated test cases fill knowledge gaps for junior testers, while seniors focus on exploratory testing and edge cases where human judgment is irreplaceable.

**Capers Jones Cross-Check (Using Plan C):**
- Total project manual estimate (all twins): ~203 + 295 + 4,003 + 130 + 394 + 1,177 = **6,202 hours**
- QA as percentage (manual): 1,177 / 6,202 = **19.0%**
- Capers Jones benchmark: 30–40%
- When adding Code Review (130 hrs) + development-embedded unit testing (~15% of dev effort = 600 hrs): total quality effort = 130 + 600 + 1,177 = 1,907 hrs = **30.8%** — within benchmark range, well-justified for healthcare/HIPAA with AI features
- AI-assisted total: ~164 + 232 + 2,943 + 101 + 294 + 1,000 = **4,734 hours** — AI saves **1,468 hours (23.7%)** across the entire project

**Final Dual Output (Plan C — Recommended):**

| Scenario | Low | Mid | High |
|---|---|---|---|
| **Manual-Only** | 942 hrs | 1,177 hrs | 1,530 hrs |
| **AI-Assisted** | 750 hrs | 1,000 hrs | 1,300 hrs |
| **AI Delta** | 192 hrs saved | 177 hrs saved | 230 hrs saved |

#### 3.6.4 Inference Rules

**Shared rules (all plans):**
- High feature count → proportionally higher test coverage effort; more FP functions → higher Dynamic_TP
- Complex business rules (from Discovery Analyst) → set Qf to 6 (thorough testing), increase Df for interconnected functions; more edge cases, more exploratory testing time
- AI-generated tests → apply AI reduction but add 15–25% review effort (someone must validate the tests themselves are correct)
- Legacy system replacement → add regression testing (30–50% of primary hours) and data migration validation effort
- Client with limited technical staff → UAT coordination takes longer (push UAT hours to upper range), set Pf toward 1.7–2.0
- Development Architect signals high integration count → increase Df for affected functions, add integration test supplementary hours
- Code Review Sentinel projects high defect density escaping to QA → increase exploratory testing percentage, set Qf to 6

**Plan-specific rules:**
- Project includes AI/LLM features → Plan A or Plan C required (Plan B cannot adequately test AI behaviors); if AI features are core to the product, weight Plan A/C recommendation heavily
- Client requires regulatory audit trail (HIPAA, SOC2, FDA) → Plan B or Plan C preferred (human QA provides documented sign-off that automated harnesses cannot); increase security/compliance supplementary hours
- Small team / startup constraints → Plan A preferred (lower ongoing headcount cost); reduce harness scope if budget-constrained
- Enterprise with existing QA organization → Plan B or Plan C preferred (leverage existing team structure); Plan C if team is willing to adopt eval tooling
- High integration count (>5 external systems) → increase Plan B team size or Plan A/C harness scope for integration test coverage
- Greenfield project with no legacy → reduce regression testing allocation across all plans; focus supplementary hours on exploratory and UAT
- Project duration >12 months → Plan A or C amortizes harness build cost more favorably; recompute 12-month cost comparison with actual timeline

**Plan selection logic:**
- If `has_ai_features AND regulatory_requirements`: recommend Plan C
- If `has_ai_features AND NOT regulatory_requirements`: recommend Plan A
- If `NOT has_ai_features AND regulatory_requirements`: recommend Plan B
- If `NOT has_ai_features AND NOT regulatory_requirements`: recommend Plan A (cost efficiency)
- Override: if `client_has_existing_qa_team`: shift recommendation one step toward Plan B/C

#### 3.6.5 Output

- **Per-plan estimates:** Effort range in hours (low/mid/high) for each of the three plans (A, B, C), with component breakdown (harness build, test execution, process overhead, supplementary)
- **Plan comparison table:** Side-by-side comparison showing manual/AI-assisted hours, upfront vs. ongoing costs, DRE, scalability, and best-fit criteria
- **Recommended plan:** With justification tied to project characteristics (AI features, regulatory requirements, team structure, timeline)
- TPA breakdown: dynamic test points, static test points, environment factor, productivity factor values used (shared across plans)
- AI-assisted reduction percentage applied per activity category, with review overhead (per plan)
- Supplementary testing breakdown per plan (performance, security, regression, UAT, exploratory)
- Capers Jones ratio cross-check result (using recommended plan)
- 12-month total cost projection per plan (including ongoing maintenance/team costs)
- Risk flags per plan (e.g., "Plan A: if AI features change rapidly, eval pipeline maintenance cost increases"; "Plan B: if team turnover occurs, knowledge transfer costs spike")
- Dependency notes on requirement completeness, code quality (from Code Review DRE projections), and deployment environment stability (from DevOps twin)

---

## 4. Orchestrator Logic

The Orchestrator is implemented as the root LangGraph StateGraph (Section 1.4.2). Its logic is distributed across several dedicated nodes in the graph. It communicates with twins using A2A `message/send` for task dispatch and receives results via A2A task completion events.

### 4.1 Context Package Assembly (`parse_input` node)

- Parse raw user input to extract structured signals (industry, tech stack, project type, integration mentions, team size hints, timeline references)
- Merge extracted signals with any structured inputs the user provided in Stages 2-3
- Package AI maturity assessment (provided or defaulted) and per-role AI effectiveness overrides (from Section 2.4.1B)
- **Historical calibration retrieval (Qdrant):** Embed the current project's scope description and structured metadata, then execute a hybrid semantic + BM25 search against Qdrant's `projects` collection (Section 6.4). For the top-3 matched past projects, traverse the Neo4j graph to retrieve their `(:Actual)` nodes, `(:Assumption)` outcomes, and per-phase variance data. Package these as calibration examples in the context
- Assemble a single A2A DataPart containing the full context package as structured JSON — this includes the `ai_role_effectiveness` matrix so each twin can compute dual estimates (AI-assisted vs. manual) with role-specific reductions, plus the Qdrant-retrieved calibration examples from similar past projects
- Distribute identical context package to all six twins via LangGraph fan-out edges — each twin receives the context as an A2A `message/send` with the DataPart payload
- If Engagement Model is "Fixed Price," inject a risk posture signal into the context package before distribution

### 4.2 Question Deduplication and Prioritization (`merge_pass1` node)

- Fan-in: LangGraph's `operator.add` reducer on `pass1_gaps` automatically merges all six twins' gap lists into a single list as they complete in parallel
- Identify overlapping information needs (e.g., "stakeholder count" and "user role count" may be answered by the same question about organizational structure)
- Score each question by estimate impact — how many twins need it and how much does it swing the overall number
- Collapse into 5-10 high-impact questions, each with a suggested default and reasoning
- Write questions to `clarifying_questions` in the LangGraph state, then hand off to the `await_user_answers` interrupt node (Stage 4 UI)
- Present to user in priority order

### 4.3 Cross-Phase Consistency Checking (`consistency_check` node)

- Runs after Pass 2 fan-in (`merge_pass2`), with access to both `pass2_estimates` and `cross_phase_signals` from the LangGraph state
- Compare twin outputs for contradictions:
  - Does the Development Architect assume clean specs while the Discovery Analyst flagged high ambiguity?
  - Does the Code Review Sentinel's estimated capacity match the Development Architect's expected output volume?
  - Does the Deployment twin assume existing CI/CD while no other twin mentioned it?
  - Does the QA twin's test scope align with the feature count from Development?
  - Does the QA twin's recommended plan (A/B/C) align with project characteristics? (e.g., Plan A recommended but no AI features detected; Plan B recommended but AI features are present)
  - Do the QA twin's three-plan cost projections maintain consistent ratios with Development effort? (QA should be 30–40% of total per Capers Jones)
- Cross-reference against A2A cross-phase signals logged in `cross_phase_signals` — if a twin received a signal but didn't adjust its estimate accordingly, flag this as an inconsistency
- When contradictions are found, the Orchestrator resolves by adjusting the less-confident twin's estimate or flagging the conflict in the output as a risk
- Optionally trigger a selective re-run: if a critical inconsistency is detected, the Orchestrator can re-invoke a specific twin via A2A `message/send` with the corrected context, without re-running all six twins. This uses LangGraph's conditional edge from `consistency_check` back to individual twin nodes

### 4.4 Commercial Parameter Processing

- The Orchestrator handles commercial parameters (Engagement Model, Role Rate Table, Budget Range, PM Overhead) directly — these are not distributed to twins because they affect cost/pricing calculations, not effort estimation
- Engagement Model drives contingency calculation: Fixed Price → add 15-30% risk buffer to the composite estimate (percentage scales with overall confidence score); T&M → no buffer but include burn-rate projections; Hybrid → apply buffer proportionally to fixed-scope portions only
- Budget Range serves as a sanity check: if the mid estimate exceeds the stated budget ceiling, the Orchestrator flags this prominently and suggests scope reduction scenarios rather than artificially compressing estimates
- PM Overhead is applied as a percentage multiplier on the composite effort total, not distributed to individual phases — this avoids double-counting since twin estimates already account for phase-specific coordination
- If Engagement Model is "Fixed Price," the Orchestrator also relays a risk posture signal to all twins in the context package: "This is a fixed-price engagement — err toward conservative assumptions and flag any ambiguity as a risk rather than resolving it optimistically"

**Role-based cost calculation (dual-scenario):**
- Each twin's output now includes **two** sets of hours attributed to four roles (see Section 3.0): an AI-assisted estimate and a manual-only estimate
- The QA & Testing Strategist is unique: it provides **three plan variants** (A/B/C), each with dual estimates. The Orchestrator uses the **recommended plan** for the composite total by default, but preserves all three for the output UI's Plan Selector (Section 5.1). The recommended plan is determined by the QA twin's inference rules (Section 3.6.4) based on project characteristics (AI features, regulatory requirements, team structure)
- The Orchestrator aggregates role-attributed hours across all six phases **for both scenarios**, then multiplies each role's total hours by its rate from the rate table
- This produces **two complete cost models**: (1) AI-assisted total cost, and (2) manual-only total cost — plus a computed "AI value delta" showing hours saved and dollars saved by role and by phase
- The AI value delta is a key output for stakeholder communication: it quantifies the ROI of AI tooling adoption and highlights which roles/phases benefit most from AI investment
- The Orchestrator also calculates a "blended effective rate" (total cost ÷ total hours) for each scenario, enabling apples-to-apples comparison

### 4.5 Estimate Synthesis

#### 4.5.1 Role Attribution Logic

The user provides project-level role effort percentages in Stage 3 (e.g., 25% Sr. Product / 25% Jr. Product / 25% Sr. Technical / 25% Jr. Technical). The Orchestrator includes these percentages in the context package distributed to all twins. Each twin then applies them to its own phase effort — but with phase-specific adjustments, because not every phase can honor the project-level split literally.

**How twins interpret project-level percentages:**

The project-level percentages set the overall staffing intent. Each twin respects that intent as closely as the nature of its phase allows, but applies a phase-specific adjustment layer. For example, if the user sets 15% Sr. Technical / 35% Jr. Technical (junior-heavy technical), the Development Architect would honor that ratio within its technical work (more junior hours on endpoint implementation, less senior architecture time), but the Discovery Analyst might override it for its phase because discovery work is inherently senior-weighted — you can't send a junior to lead stakeholder interviews.

Each twin documents its actual role split in its output alongside the user's requested split, with a justification when they diverge:

| Phase | Adjustment Behavior |
|---|---|
| Discovery | Strongly senior-biased regardless of input — caps Jr. roles at ~25% combined. If user requested 60% junior, twin applies 25% and flags: "Discovery requires senior-led stakeholder engagement; junior allocation capped." |
| UX/Design | Product-biased regardless of input — if user requested 0% Product roles, twin flags infeasibility. Respects Jr/Sr ratio within product roles. Technical % honored for constraint review work. |
| Development | Honors input percentages most faithfully — this phase has genuine work for all four roles. Adjusts only if a percentage would leave a role with <8 hours (below minimum viable allocation). |
| Code Review | Senior-biased within each track — review is inherently senior work. Jr. percentages apply only to revision-addressing time on their own PRs, not to review activity itself. |
| Deployment | Technical-biased regardless of input — if user requested 60% product roles, twin reassigns the deployment-specific work to technical roles and flags the deviation. Product roles get only release validation and UAT coordination. |
| QA/Testing | Honors input percentages well — testing has genuine work for all four roles. Adjusts Product/Technical split based on whether test work is UI-focused or system-focused. |

**When user skips role percentages entirely:** Defaults to 25/25/25/25 (balanced). The twins apply the phase-specific adjustments above, which naturally weight each phase toward its appropriate roles.

#### 4.5.2 Headcount Recommendation (Twin-Generated Output)

Team headcount is NOT a user input — it is derived by the Orchestrator from the twins' effort estimates and the target timeline. This avoids circular reasoning (effort depends on team capability, but team size depends on effort).

**Calculation:**
1. After Pass 2, the Orchestrator has total role-attributed hours across all six phases (low/mid/high)
2. Using the target timeline from Stage 2 (or a default if not specified), calculate required capacity per role:
   - `required_headcount[role] = role_total_hours_mid / (timeline_weeks × hours_per_week_per_person)`
   - Default `hours_per_week_per_person`: 32 (accounting for meetings, context-switching, and non-project time — not 40)
3. Round up to whole people (you can't hire 1.3 engineers)
4. Apply minimum viability constraints:
   - If any role's calculated headcount is >0 but <1, round up to 1 (need at least one person in a role if there are hours for it)
   - If a role has 0 hours (because the user set that role's percentage to 0%), headcount is 0
5. Cross-check against phase parallelism: some phases run concurrently (Development and UX overlap, QA overlaps with late Development). The Orchestrator checks whether the recommended team can actually staff the concurrent phases — if not, it suggests increasing headcount for the bottleneck role

**Output includes:**
- Recommended headcount per role with justification
- Utilization percentage per role (how fully loaded each person would be — 85-95% is healthy, >95% is risky, <70% suggests the role could be covered by a fractional resource or merged with another role)
- Alternative staffing scenarios: "If you add 1 more Sr. Technical, duration decreases from 16 to 13 weeks" / "If you remove 1 Jr. Product, duration increases from 16 to 19 weeks but cost decreases by $X"

#### 4.5.3 Aggregation

- Aggregate all six phase estimates into a composite project estimate, preserving role attribution
- Per-role hour totals: sum each role's hours across all six phases (low/mid/high ranges), with actuals vs. user-requested percentages tracked for transparency
- Apply cross-phase adjustments (cascading effects, dependency buffers) — distribute adjustment hours across roles using the user's input percentages
- Apply commercial adjustments (contingency buffer based on engagement model, PM overhead) — distribute proportionally across roles using input percentages
- Calculate cost ranges: multiply each role's hours by its rate from the rate table, sum for total project cost
- Derive recommended headcount per role (Section 4.5.2)
- Calculate duration ranges: divide total effort by derived team capacity, apply parallelism factor (product and technical work often run in parallel across phases)
- Calculate weekly/monthly burn rate by role — particularly important for T&M engagements where the client needs to know their ongoing spend commitment
- Produce the final structured output including: effort by role, cost by role, recommended headcount by role, total effort, total cost, duration, burn rate, assumptions, risks, and confidence score

---

## 5. Output Design

### 5.1 Proposal-Ready Estimate

The primary estimate view presents **dual scenarios** (AI-assisted vs. manual-only) with a toggle to switch between them. The default view shows the AI-assisted estimate with the AI value delta displayed as a persistent summary bar.

- **Scenario Toggle (primary UI control):**
  - **AI-Assisted** (default) — shows estimates with per-role AI effectiveness reductions applied
  - **Manual-Only** — shows estimates with all AI reductions set to 0%
  - **Side-by-Side** — shows both scenarios in adjacent columns for direct comparison
  - A persistent "AI Value Delta" summary bar appears at the top: "AI saves X hours / $Y across this project" with a breakdown link

- **Phase-by-phase breakdown** with effort ranges (hours) per role:
  - Each phase shows total hours (low/mid/high) and a sub-table attributing hours to Sr. Product, Jr. Product, Sr. Technical, Jr. Technical — **for the currently selected scenario**
  - Per-phase cost calculated by multiplying each role's hours by its rate
  - Per-phase AI delta badge: "AI saves X hrs / $Y in this phase" (visible in AI-Assisted and Side-by-Side views)
  - **QA & Testing phase includes a Plan Selector** (Plan A / Plan B / Plan C) with the recommended plan pre-selected based on project characteristics. Switching plans updates the QA hours, the cross-phase totals, Capers Jones cross-check, and the overall project total in real time. A comparison view shows all three plans side-by-side with upfront vs. ongoing cost projections
- **Role summary across all phases:**
  - Total hours per role (low/mid/high) — for selected scenario
  - Total cost per role
  - Blended effective rate (total cost ÷ total hours) for clients who prefer a single-rate contract
  - Weekly/monthly burn rate by role — shows the ongoing spend commitment, especially important for T&M engagements
- **AI Value Impact Analysis (new section):**
  - Total hours saved by AI: breakdown by phase and by role
  - Total cost saved: by phase and by role, using the role rate table
  - **Senior resource impact highlight:** separately calls out the dollar impact on senior roles vs. junior roles — because senior hours are 2–3× more expensive, the same percentage reduction translates to disproportionate cost savings
  - Maturity ceiling impact: "At your current AI Maturity Level 3, you're capped at 30% reduction. Moving to Level 4 would save an additional X hours / $Y" — actionable recommendation for AI investment
  - Per-phase AI effectiveness: which phases benefit most/least from AI, helping stakeholders prioritize AI adoption efforts
- **Staffing plan (twin-generated recommendation):**
  - Recommended headcount per role — derived from role-attributed effort and target timeline (Section 4.5.2), not user-provided. **Shown for both scenarios** — AI-assisted staffing may require fewer people
  - Utilization percentage per role — how fully loaded each person would be (85-95% healthy, >95% risky, <70% consider fractional/merged)
  - Role percentage comparison: user's requested distribution vs. what the twins actually applied per phase, with flags where they diverged and why
  - Alternative staffing scenarios with cost/duration tradeoffs: "Add 1 Sr. Technical → duration -3 weeks, cost +$X" / "Remove 1 Jr. Product → duration +3 weeks, cost -$Y"
- Per-phase assumption list — what was inferred, what was provided, what was defaulted
- Per-phase risk register — what could change the estimate and by how much
- Cross-phase dependency map — which phases are tightly coupled and where bottleneck risk exists
- AI maturity impact summary — how the client's current AI adoption level affected the estimate, and what changes if they invest in AI readiness before the project starts. Now includes the concrete delta calculation: "Current estimate assumes Level 3. At Level 4, you'd save an additional X hours / $Y."
- **Junior/Senior mix impact summary** — shows how the current ratio affects cost vs. duration tradeoff, with a "what-if" toggle: "If you shifted 20% of Jr. hours to Sr., cost increases by $X but duration decreases by Y weeks." **Now also shows how the shift interacts with AI effectiveness:** shifting hours to seniors who have higher AI effectiveness may partially offset the cost increase

### 5.2 Output Formats

- Structured display within the application (primary view for review and editing)
- Export to a format that can be pasted or imported into proposal documents (markdown, structured text, or direct integration with proposal templates)
- Consider a summary view (one-page high-level estimate for quick reference) and a detail view (full breakdown with all assumptions and risks)

### 5.3 Adjustability

- After the estimate is generated, allow manual overrides on any phase — the user should be able to say "I know this client, discovery will be lighter than you think" and adjust the number
- When a manual override is made, flag any downstream impacts ("reducing discovery effort may increase risk of spec gaps — Development and UX phases may need adjustment")
- Track which parts of the estimate are model-generated versus manually adjusted

---

## 6. Data Model and Calibration

### 6.1 Neo4j Graph Data Model

The application's data model is stored in Neo4j as a property graph. The graph structure captures the provenance chain of every estimate — from raw input through twin reasoning to final output — and connects estimates to historical actuals for calibration.

**Core node types:**

```cypher
// Project and estimation nodes
(:Project {id, name, created_at, status})
(:Estimate {id, version, created_at, confidence_score, scenario: "ai_assisted"|"manual"})
(:Phase {name, low_hrs, mid_hrs, high_hrs, ai_delta_hrs})
(:Twin {name, algorithm, pass1_gaps, pass2_output_json})
(:CrossPhaseSignal {source_twin, target_twin, signal_type, payload_json, timestamp})

// Historical and calibration nodes
(:Actual {phase, actual_hrs, recorded_at, variance_pct})
(:Assumption {text, proved_correct: boolean, impact_description})
(:Risk {description, materialized: boolean, actual_impact_hrs})

// Client and profile nodes
(:ClientProfile {industry, size_band, ai_maturity, project_type, tech_stack_category})
(:User {id, email, role})

// QA plan nodes (unique to QA twin)
(:QAPlan {plan_type: "A"|"B"|"C", manual_hrs, ai_hrs, recommended: boolean, justification})
```

**Core relationships:**

```cypher
(:User)-[:CREATED]->(:Project)
(:Project)-[:HAS_ESTIMATE]->(:Estimate)
(:Estimate)-[:INCLUDES_PHASE]->(:Phase)
(:Phase)-[:PRODUCED_BY]->(:Twin)
(:Twin)-[:SIGNALED {signal_type}]->(:Twin)
(:Twin)-[:EMITTED]->(:CrossPhaseSignal)
(:CrossPhaseSignal)-[:RECEIVED_BY]->(:Twin)
(:Estimate)-[:CALIBRATED_BY]->(:Actual)
(:Phase)-[:HAS_ACTUAL]->(:Actual)
(:Phase)-[:ASSUMED]->(:Assumption)
(:Phase)-[:IDENTIFIED_RISK]->(:Risk)
(:Project)-[:MATCHES_PROFILE]->(:ClientProfile)
(:Project)-[:SIMILAR_TO {similarity_score}]->(:Project)
(:Phase)-[:HAS_QA_PLAN]->(:QAPlan)  // QA phase only
```

**Example calibration queries:**

```cypher
// Find all regulated-industry projects where Discovery underestimated by >20%
MATCH (p:Project)-[:MATCHES_PROFILE]->(cp:ClientProfile {industry: "healthcare"})
MATCH (p)-[:HAS_ESTIMATE]->(e:Estimate)-[:INCLUDES_PHASE]->(ph:Phase {name: "discovery"})
MATCH (ph)-[:HAS_ACTUAL]->(a:Actual)
WHERE a.variance_pct > 20
RETURN p.name, ph.mid_hrs AS estimated, a.actual_hrs AS actual, a.variance_pct

// Trace the full provenance chain for a specific estimate
MATCH path = (u:User)-[:CREATED]->(p:Project)-[:HAS_ESTIMATE]->(e:Estimate)
  -[:INCLUDES_PHASE]->(ph:Phase)-[:PRODUCED_BY]->(t:Twin)
WHERE e.id = $estimateId
OPTIONAL MATCH (t)-[:EMITTED]->(sig:CrossPhaseSignal)-[:RECEIVED_BY]->(t2:Twin)
RETURN path, sig, t2

// Find which twins are most/least accurate for a given client profile
MATCH (cp:ClientProfile {industry: $industry, size_band: $size})
MATCH (p:Project)-[:MATCHES_PROFILE]->(cp)
MATCH (p)-[:HAS_ESTIMATE]->(e)-[:INCLUDES_PHASE]->(ph)-[:PRODUCED_BY]->(t:Twin)
MATCH (ph)-[:HAS_ACTUAL]->(a:Actual)
RETURN t.name, avg(a.variance_pct) AS avg_variance, count(a) AS sample_size
ORDER BY avg_variance DESC
```

### 6.2 Historical Project Data

- After project completion, create `(:Actual)` nodes linked to each `(:Phase)` node in the estimate graph:
  - Per-phase actual effort versus estimated effort (variance percentage auto-calculated)
  - `(:Assumption)` nodes recording which inferences proved correct and which were wrong
  - `(:Risk)` nodes recording what risks materialized and what the actual impact was
  - `(:ClientProfile)` node capturing industry, size, AI maturity, project type for pattern matching
- This data becomes the training/calibration set for improving twin inference over time
- The graph structure makes it trivial to traverse from an inaccurate estimate back through the twin that produced it, the cross-phase signals it received, and the assumptions it made — enabling root-cause analysis of estimation errors

### 6.3 Calibration Mechanism

- After each completed project, feed actuals back into the system by creating `(:Actual)` and `(:Assumption)` nodes linked to the original estimate graph
- Adjust twin inference weights based on graph-derived patterns in estimation accuracy:
  - "For regulated-industry clients, we consistently underestimate discovery by 20% — increase default weighting" (detected via Cypher aggregation over `(:ClientProfile {industry: 'healthcare'})-[:MATCHES_PROFILE]-(p)-[:HAS_ESTIMATE]-(e)-[:INCLUDES_PHASE]-(ph {name: 'discovery'})-[:HAS_ACTUAL]-(a)`)
  - "When clients report having documentation, it's accurate 60% of the time — don't reduce effort as aggressively" (detected via `(:Assumption {text: 'client documentation available', proved_correct: false})` frequency)
- Calibration updates propagate through three channels: (1) adjusted few-shot examples injected into twin prompts via LangChain's `FewShotChatMessagePromptTemplate`, dynamically selected from the most similar past projects retrieved by Qdrant; (2) twin-specific bias correction factors stored as properties on `(:Twin)` nodes and applied as multipliers during Pass 2; (3) updated inference rules when patterns are strong enough to codify
- Confidence score increases as more `(:Actual)` nodes accumulate for a given `(:ClientProfile)` pattern — the system tracks sample size per profile type and reports calibration confidence alongside estimates

### 6.4 Vector Search Architecture (Qdrant)

Qdrant provides hybrid semantic + BM25 search over historical project data, enabling the orchestrator to find the most relevant past projects for calibration context injection.

**Collections:**

| Collection | Embedding Source | Dense Model | Sparse Model | Use Case |
|---|---|---|---|---|
| `projects` | Concatenated scope description + structured metadata (industry, tech stack, project type, AI maturity) | `text-embedding-3-small` (1536d) | Built-in BM25 via Qdrant's sparse vector support | Project-level similarity: "find past projects most like this one" |
| `phase_estimates` | Per-phase twin output (algorithm used, key assumptions, effort breakdown) | `text-embedding-3-small` (1536d) | Built-in BM25 | Twin-level matching: "find past Discovery estimates for similar regulated-industry portals" |
| `client_profiles` | Client characteristic text (industry description, team structure, AI maturity narrative) | `text-embedding-3-small` (1536d) | Built-in BM25 | Profile template matching: "find clients with a similar profile to this one" |

**Hybrid search strategy:**

Each query combines dense and sparse vectors using Reciprocal Rank Fusion (RRF) to get the best of both approaches:
- **Dense (semantic):** Captures conceptual similarity — a "patient scheduling system" matches "appointment booking platform" even though the words differ
- **Sparse (BM25):** Captures exact keyword matches — ensures that specific tech stack names ("Spring Boot," "Terraform"), compliance frameworks ("HIPAA," "SOC2"), and industry terms ("EHR integration") are matched precisely
- **Fusion weight:** Default 0.6 dense / 0.4 sparse, configurable per collection. Higher sparse weight for `phase_estimates` (technical details matter more) vs. higher dense weight for `client_profiles` (conceptual similarity matters more)

**Integration with the orchestrator:**

The `parse_input` node (Section 4.1) performs a Qdrant hybrid search as part of context assembly:

1. Embed the current project's scope description and structured metadata
2. Query the `projects` collection with hybrid search (top-k=5)
3. For each matched project, traverse the Neo4j graph to retrieve its `(:Actual)` nodes and `(:Assumption)` outcomes
4. Package the top-3 most relevant past projects (with their estimate-vs-actual variance data) into the context package as calibration examples
5. Each twin receives these calibration examples alongside the standard context, enabling it to adjust its estimates based on how similar past projects actually landed

**Indexing pipeline:**

When a project is completed and actuals are recorded:
1. Generate embeddings for the project scope, per-phase outputs, and client profile
2. Upsert into the corresponding Qdrant collections with metadata filters (industry, project_type, ai_maturity_level, completion_date)
3. Create `(:Project)-[:SIMILAR_TO {similarity_score}]->(:Project)` relationships in Neo4j for the top-5 nearest neighbors (pre-computed for fast graph traversal)

**Metadata filtering:**

Qdrant's payload filtering narrows the search space before vector similarity is computed:
- `industry IN ["healthcare", "fintech"]` — restrict to relevant industries
- `ai_maturity_level >= 2` — only match projects with comparable AI adoption
- `completion_date > "2024-01-01"` — prefer recent projects for relevance
- `project_type = "greenfield"` — match on project category

### 6.5 Project Profile Templates

- Over time, cluster completed projects into profile types using both Neo4j community detection (Louvain algorithm on the `(:Project)-[:SIMILAR_TO]->(:Project)` graph) and Qdrant cluster analysis
- Profile types emerge organically: "regulated industry legacy replacement," "greenfield SaaS build," "mobile app with API integrations" — stored as `(:ClientProfile)` nodes with aggregate statistics (mean effort per phase, typical variance, common risk patterns)
- When a new project matches a profile (via Qdrant similarity search against the `client_profiles` collection), pre-populate twin inferences with the historical baseline for that profile type
- Allow the user to select a starting template ("this looks most like our Exsif project") to jumpstart the estimation — the UI queries Neo4j for the selected project's full estimate graph and uses it as a starting point

---

## 7. Technical Implementation Considerations

### 7.1 Application Architecture

#### 7.1.1 Frontend: Next.js

**Decision: Next.js** (App Router) as the UI framework.

**Why Next.js:**
- **Server Components for the input wizard:** Stages 1-3 of the input flow (Section 2) are form-heavy with minimal client interactivity until submission — React Server Components render these stages with zero client-side JavaScript bundle cost, improving initial load performance
- **Server Actions for form submission:** Each stage transition (Stage 1 → AI parsing → Stage 2 pre-population) maps cleanly to Next.js Server Actions — the AI parsing pass runs server-side, pre-populates Stage 2 fields, and returns the rendered form without a separate API endpoint
- **Streaming with Suspense for twin execution:** During Pass 1 and Pass 2, the backend processes six twins in parallel. Next.js's streaming SSR with `<Suspense>` boundaries enables progressive rendering — the UI can show per-twin progress indicators that resolve individually as each twin completes, rather than waiting for all six before rendering anything
- **Route-based code splitting:** Each stage of the input wizard (and the estimate review view) maps to a route segment (`/estimate/new`, `/estimate/[id]/context`, `/estimate/[id]/maturity`, `/estimate/[id]/questions`, `/estimate/[id]/review`), giving automatic code splitting and clean URL-addressable states. Users can bookmark or share a link to return to Stage 4 after stepping away
- **SSE consumption for real-time updates:** Next.js client components consume Server-Sent Events from the LangGraph Agent Server for real-time twin progress and A2A task status updates during estimation runs. The EventSource API in client components connects directly to the backend's SSE endpoints
- **API routes as a BFF layer:** Next.js API routes (`/api/estimates/*`) act as a Backend-for-Frontend layer between the browser and the LangGraph Agent Server — handling authentication, request validation, and response shaping without exposing the LangGraph API surface directly to the client

**Key pages and route structure:**

```
app/
├── page.tsx                              # Dashboard: recent estimates, quick actions
├── estimate/
│   ├── new/
│   │   └── page.tsx                      # Stage 1: Raw Input Capture
│   └── [id]/
│       ├── context/
│       │   └── page.tsx                  # Stage 2: Project Context & Parameters
│       ├── maturity/
│       │   └── page.tsx                  # Stage 3: AI Maturity & Team Assessment
│       ├── questions/
│       │   └── page.tsx                  # Stage 4: Clarifying Questions
│       ├── review/
│       │   └── page.tsx                  # Stage 5: Estimate Review & Adjustment
│       └── explain/
│           └── page.tsx                  # Reasoning chain explorer (Langfuse trace view)
├── history/
│   └── page.tsx                          # Estimate history, comparison, calibration data
└── api/
    └── estimates/
        ├── route.ts                      # POST: create new estimate, GET: list estimates
        ├── [id]/
        │   ├── route.ts                  # GET: fetch estimate state, PATCH: manual overrides
        │   ├── context/route.ts          # POST: submit Stage 2 data
        │   ├── maturity/route.ts         # POST: submit Stage 3 data
        │   ├── answers/route.ts          # POST: submit Stage 4 answers, resume LangGraph
        │   ├── export/route.ts           # GET: export as XLSX/markdown/clipboard format
        │   └── stream/route.ts           # GET: SSE endpoint proxying twin execution progress
        └── templates/
            └── route.ts                  # GET: list project profile templates
```

**Frontend dependencies:**
- `next` (App Router, React 19+)
- `tailwindcss` — utility-first styling for the input forms and estimate display
- `react-hook-form` + `zod` — form management and validation for the 42-field input schema (Section 2.7), with Zod schemas shared between frontend validation and API route validation
- `recharts` or `visx` — data visualization for the phase breakdown bar chart, confidence meter, and Cone of Uncertainty indicator in Stage 5
- `@tanstack/react-query` — client-side data fetching, caching, and optimistic updates for estimate adjustments
- `nuqs` — URL search parameter state management for filter/sort state on the history and comparison views

#### 7.1.2 Backend: LangGraph Agent Server

- **LangGraph Agent Server (Python)** hosting the Orchestrator StateGraph. Each twin runs as an A2A-addressable agent within the server. The Agent Server exposes A2A endpoints (`/a2a/{assistant_id}`) for each twin and SSE streams for execution progress
- The Next.js API routes proxy requests to the LangGraph Agent Server, translating between the frontend's REST/SSE contract and LangGraph's native API. This decouples the frontend from LangGraph internals — if the orchestration framework changes in the future, only the API route layer needs updating
- LangGraph's `await_user_answers` interrupt persists graph state to the checkpointer when the user reaches Stage 4. The Next.js `answers/route.ts` API route resumes the graph by posting the user's answers, which triggers Pass 2 execution

#### 7.1.3 State Persistence & Database

- **Graph database: Neo4j** — serves as the primary persistence layer for the application's interconnected data model. Neo4j's property graph model is a natural fit for estimation data because project estimates are inherently relational: projects connect to phases, phases connect to twins, twins emit cross-phase signals to other twins, estimates reference historical projects, and calibration data links actuals back to estimates. A graph database makes these relationships first-class citizens rather than join-table afterthoughts.
  - **LangGraph checkpointer:** Neo4j-backed via a custom checkpointer adapter (LangGraph supports custom checkpoint backends). Persists graph state during the human-in-the-loop pause (Stage 4) and enables resume after browser close/reopen. Each estimation run has a unique thread ID that maps to the Next.js route parameter `[id]`. State snapshots are stored as serialized nodes with `NEXT_STATE` edges for version history
  - **Application data model:** Project estimates, historical actuals, calibration data, user adjustments, and user accounts are stored as Neo4j nodes and relationships. Key node types: `(:Project)`, `(:Estimate)`, `(:Phase)`, `(:Twin)`, `(:CrossPhaseSignal)`, `(:Actual)`, `(:ClientProfile)`, `(:User)`. Relationships capture the estimation provenance chain: `(:Estimate)-[:PRODUCED_BY]->(:Twin)`, `(:Twin)-[:SIGNALED]->(:Twin)`, `(:Estimate)-[:CALIBRATED_BY]->(:Actual)`, `(:Project)-[:MATCHES_PROFILE]->(:ClientProfile)`
  - **Graph queries for calibration:** Cypher queries enable powerful calibration lookups that would require complex multi-table joins in a relational model: "find all past projects where the Discovery twin underestimated by >20% AND the client was in a regulated industry AND AI maturity was Level 2-3" becomes a single pattern match traversal
  - **ORM:** `neo4j-driver` (Python) for the LangGraph Agent Server; `neo4j-driver` (JavaScript) or `neogma` for the Next.js layer

- **Vector database: Qdrant** — provides semantic and keyword (BM25) hybrid search over historical project data for the calibration and profile matching features (Section 6). See Section 6.4 for the full vector search architecture.
  - At estimate creation time, the orchestrator queries Qdrant to find the most similar past projects and injects their actuals as calibration context for the twins
  - Supports both dense vector search (semantic similarity via embeddings) and sparse vector search (BM25 keyword matching) in a single query, enabling hybrid retrieval that catches both conceptually similar projects and projects with exact keyword matches (e.g., specific tech stack names, industry terms, compliance frameworks)
  - Collections: `projects` (project-level embeddings from scope descriptions + structured metadata), `phase_estimates` (per-phase embeddings for fine-grained twin-level matching), `client_profiles` (client characteristic embeddings for profile template matching)

#### 7.1.4 Observability & Infrastructure

- **Observability:** Langfuse for LLM/agent tracing, evaluation, and prompt management (open-source, self-hosted via Docker/Kubernetes or cloud-hosted). Framework-agnostic with OpenTelemetry-based tracing and native LangChain integration. Next.js application monitoring via Vercel Analytics or OpenTelemetry
- **Infrastructure:** Containerized deployment (Docker Compose for development, Kubernetes or similar for production) with five services: Next.js frontend (Node.js), LangGraph Agent Server (Python), Neo4j (graph database), Qdrant (vector database), and Langfuse (observability). Consider Vercel for the Next.js frontend, LangGraph Cloud for the agent server, Neo4j AuraDB for managed graph hosting, and Qdrant Cloud for managed vector search if managed deployment is preferred over self-hosting

### 7.2 Agent Framework: LangGraph + A2A Protocol

**Decision: LangGraph** (from LangChain) as the orchestration framework, with **A2A** (Agent-to-Agent protocol) for inter-agent communication.

**Why LangGraph over alternatives (CrewAI, AutoGen, custom):**
- **StateGraph with typed state:** LangGraph's `StateGraph` with `TypedDict` state and reducer annotations (`Annotated[list, operator.add]`) maps directly to our fan-out/fan-in pattern — six twins write to the same state key in parallel, and LangGraph merges the results automatically. CrewAI and AutoGen don't offer this level of state management precision
- **Static fan-out for deterministic parallel execution:** Our twin topology is fixed (always six twins), so LangGraph's static fan-out (multiple edges from one node) is cleaner than dynamic `Send()` API. Each pass is a single superstep — all six twins execute concurrently and the graph waits for all to complete before proceeding
- **Human-in-the-loop as a first-class primitive:** LangGraph's interrupt/resume with checkpointing handles the Stage 4 pause naturally. The graph state persists while the user answers clarifying questions, and resumes exactly where it left off — no custom session management needed
- **Conditional edges for selective re-estimation:** After consistency checking, the Orchestrator can conditionally route back to specific twin nodes for re-estimation without restarting the entire graph
- **Built-in observability:** LangGraph integrates with Langfuse via its native LangChain callback handler for tracing every node execution, state transition, and LLM call — essential for the "explain this estimate" feature and calibration debugging

**Why A2A over custom message schemas:**
- **Standardized task lifecycle** (`submitted` → `working` → `completed`/`failed`) gives the UI layer a consistent progress model for free
- **Native LangGraph support:** LangGraph Agent Server exposes A2A endpoints at `/a2a/{assistant_id}` (requires `langgraph-api>=0.4.21`), so twins can be addressed as A2A agents without additional infrastructure
- **SSE streaming for cross-phase signals:** A2A's Server-Sent Events support enables the reactive parallel execution model — upstream twins stream signals to downstream twins in real time during Pass 2 without waiting for full completion
- **Extensibility:** Adding a new twin or connecting to an external estimation agent requires only publishing a new Agent Card — no Orchestrator code changes for discovery. The `crossPhaseSubscriptions`/`crossPhasePublishes` extension fields in Agent Cards declare the signal topology declaratively

**Key dependencies:**
- `langchain-core` — base abstractions for LLM calls and prompt templates
- `langgraph` — StateGraph, fan-out/fan-in, interrupts, checkpointing
- `langgraph-api>=0.4.21` — Agent Server with A2A endpoint support
- `langgraph-a2a-client` — A2A client library for agent discovery and message sending
- `langfuse` — open-source observability and tracing (MIT license, self-hostable via Docker/Kubernetes); provides LLM tracing, prompt management, evaluation scoring, and dataset tools without vendor lock-in; integrates with LangChain/LangGraph via native callback handler
- `deepeval` — LLM evaluation framework used by the QA & Testing Strategist's Plan A and Plan C eval harness calculations; provides metric primitives (faithfulness, relevance, hallucination, toxicity, bias), synthetic test data generation, LLM-as-judge scaffolding, and CI/CD eval gate integration; referenced in Section 3.6.2A and the worked example (Section 3.6.3) for harness build effort sizing
- `neo4j-driver` — official Neo4j driver (Python for Agent Server, JavaScript for Next.js); provides Bolt protocol connectivity to the Neo4j graph database for application state persistence, LangGraph checkpointing, and calibration graph traversals
- `qdrant-client` — official Qdrant Python client for hybrid semantic + BM25 search over historical project data; used by the orchestrator's `parse_input` node to retrieve similar past projects for calibration context injection (Section 6.4)
- LangGraph checkpointer backend: Neo4j (production) via custom checkpoint adapter or SQLite (development) for persisting graph state during human-in-the-loop pauses

### 7.3 Prompt Management

- Seven distinct system prompts (one per twin + orchestrator) managed as LangChain `ChatPromptTemplate` objects with composable sections
- Each twin's prompt is structured into composable components: persona definition, evaluation dimensions, inference rules, output format schema, and A2A signal handling instructions (what cross-phase signals to listen for, how to incorporate them, what signals to emit, and the DataPart payload schema for each)
- Prompts are version-controlled alongside their corresponding Agent Card definitions — a prompt version change triggers an Agent Card version bump
- Prompts will evolve as calibration data accumulates and as the AI SDLC methodology itself evolves
- Use Langfuse's prompt versioning and evaluation features to test prompt changes against historical estimates before deploying — run the updated twin against a suite of past project inputs and compare outputs to known actuals
- Inject few-shot examples from the calibration data store (Section 6) directly into prompts using LangChain's `FewShotChatMessagePromptTemplate` — dynamically selected via Qdrant hybrid search (Section 6.4) based on how closely the current project profile matches historical profiles, with actuals retrieved from Neo4j's estimate provenance graph

### 7.4 Error Handling and Fallbacks

- **Twin failure during parallel execution:** If a twin's LLM call fails or times out, LangGraph's fan-in still waits for all six nodes. The failed twin node should catch the exception internally and return a structured error result (effort range: null, error: "LLM timeout", fallback: true) so the fan-in merger can proceed. The Orchestrator flags the failed phase in the output and either retries that specific twin via a conditional edge or presents the estimate with the failed phase marked as "manual input required"
- **A2A signal timeout:** If an upstream twin fails to emit its cross-phase signal within the 15-second window (Section 1.3.3), downstream twins proceed without the signal and state the assumption explicitly. The A2A task status for the upstream twin transitions to `failed`, which the Orchestrator logs for debugging
- **Incoherent twin output:** The `merge_pass1` and `merge_pass2` nodes validate each twin's output against its expected JSON schema (defined in the twin's Agent Card). If validation fails, the Orchestrator re-invokes the twin with a tighter prompt requesting structured output
- **Input too vague:** If all six twins return high-uncertainty flags in Pass 1 (e.g., all gap lists exceed 5 items each), the Orchestrator should recognize this and request more information via additional clarifying questions rather than producing a low-confidence estimate
- **LLM API failures:** Standard resilience patterns — retries with exponential backoff (max 3 attempts per twin), circuit breaker per LLM provider, graceful degradation (skip the failed twin and flag it). LangGraph's checkpointing ensures that retrying a single twin doesn't re-execute the entire graph

### 7.5 Observability and Debugging

- **Langfuse integration:** Every LangGraph node execution, LLM call, state transition, and A2A message exchange is automatically traced in Langfuse via its LangChain callback handler. Each estimation run produces a single trace with nested spans for: input parsing → Pass 1 (six parallel twin spans) → question generation → user interaction → Pass 2 (six parallel twin spans + A2A signal spans) → consistency check → synthesis
- **A2A signal audit trail:** All cross-phase signals exchanged between twins in Pass 2 are logged in the `cross_phase_signals` state key — this provides a complete record of which twins influenced which other twins' estimates and with what data
- **Full reasoning chain:** raw input → context package → Pass 1 twin outputs → clarifying questions → user answers → A2A cross-phase signals → Pass 2 twin outputs → consistency adjustments → commercial adjustments → final estimate. Every step is a Langfuse span with input/output data
- **"Explain this estimate" feature:** The UI can query the Langfuse trace via its API for a specific estimation run and render the reasoning chain as an expandable tree — the user can trace any number back to its source logic, including which cross-phase A2A signal from which twin influenced a particular assumption
- **Calibration feedback loop:** When a project completes and actuals are recorded (Section 6), Langfuse traces from the original estimation run can be compared against actuals to identify which twins were most/least accurate and which cross-phase signals improved or degraded estimate quality

---

## 8. User Experience

### 8.1 Workflow

The user-facing workflow follows the four-stage progressive disclosure pattern defined in Section 2, with AI processing steps between stages:

1. **Stage 1 — Raw Input:** User opens the tool and pastes/types raw project input (free text) and optionally uploads supporting files. This is the only required step. User can choose "Skip to Quick Estimate" to bypass Stages 2-3 entirely.
2. **AI Parsing Pass:** System analyzes raw input to extract structured signals (2-5 seconds, loading indicator). Pre-populates Stage 2 fields with AI-extracted values.
3. **Stage 2 — Project Context:** User reviews and corrects AI-extracted fields (industry, project type, integrations, etc.) and optionally fills in additional structured parameters (engagement model, technical environment, NFRs). All fields are optional and use progressive disclosure (collapsed sections for lower-priority categories).
4. **Stage 3 — AI Maturity & Team:** User rates AI maturity across six phases and optionally provides team profile data. Defaults to conservative assumptions (Level 1 / No AI) when skipped.
5. **Pass 1 Processing:** Orchestrator assembles the full context package from Stages 1-3 and distributes to all six twins via LangGraph fan-out. Twins run as a single superstep and return preliminary estimates plus gap analyses. (Target: 10-20 seconds, per-twin progress indicators via A2A task status events.)
6. **Stage 4 — Clarifying Questions:** System presents 5-10 high-impact clarifying questions with suggested defaults. User answers what they can, skips the rest with "Use defaults for remaining." Graph execution is paused via LangGraph interrupt with checkpointed state.
7. **Pass 2 Processing:** Orchestrator redistributes enriched context via LangGraph fan-out. Twins run in parallel with reactive A2A cross-phase signaling — upstream twins stream signals to downstream twins via SSE. Consistency check runs after fan-in. (Target: 15-30 seconds, slightly longer than Pass 1 due to A2A signal exchange latency.)
8. **Stage 5 — Estimate Review:** System presents the full estimate — summary view first (effort/cost/duration ranges, confidence score, Cone of Uncertainty indicator), detail view expandable per phase. User reviews assumptions, adjusts numbers manually (with downstream impact warnings), and exports for the proposal.

### 8.2 Time-to-Estimate Target

- The entire workflow (input → questions → final estimate) should complete in under 10 minutes of user time
- LLM processing time (both passes combined) should target under 3 minutes
- The clarifying question step is where the user spends the most time — keep it efficient

### 8.3 Team Collaboration

- Should multiple team members be able to contribute to the same estimate? (e.g., a tech lead adjusts the Development phase while a PM adjusts Discovery)
- Version history on estimates — track changes over time as a proposal evolves
- Commenting or annotation capability on specific assumptions or risk flags

---

## 9. Risks and Open Questions

### 9.1 Synthetic Agreement Problem

- All six twins share the same underlying LLM — they may converge on similar blind spots rather than providing genuinely independent perspectives
- Mitigation: vary prompting strategies across twins, inject deliberate contrarian reasoning in at least one twin, compare estimates against historical actuals to detect systematic bias

### 9.2 Cold Start Problem

- With no historical project data, the twins are estimating from general knowledge and the encoded rules in their prompts
- The tool will be least accurate when you need it most (early on) and most accurate when you need it least (after many projects)
- Mitigation: seed Neo4j and Qdrant with retrospective data from past projects — even rough actuals are better than nothing. Create `(:Project)` → `(:Estimate)` → `(:Phase)` → `(:Actual)` graphs from historical records and embed project descriptions into Qdrant collections. A seed script should accept CSV/JSON exports of past project data and build the graph + vector index in a single batch operation
- Mitigation: until sufficient historical data accumulates, Qdrant searches will return few or no matches — the orchestrator gracefully degrades by omitting calibration examples from the context package and noting "no similar past projects found" in the output

### 9.3 Overfitting to Past Projects

- As calibration data accumulates in Neo4j, twins might become too tuned to your specific project history and fail on novel project types
- Mitigation: maintain general inference rules as a baseline and layer calibration adjustments on top rather than replacing the baseline entirely. Qdrant similarity scores below a configurable threshold (default 0.65) are excluded from calibration injection — if a new project is truly novel, the twins fall back to general knowledge rather than being misled by superficially similar but fundamentally different past projects

### 9.4 Estimate Anchoring

- Users may anchor on the first estimate they see and make only minor adjustments, even when the estimate is wrong
- Mitigation: present ranges rather than point estimates, highlight low-confidence assumptions prominently, encourage review of the assumption list before accepting the numbers

### 9.5 Scope of "Cost"

- Is the estimate purely effort (hours)? Or does it include infrastructure costs, tooling licenses, third-party service costs?
- Does it account for project management overhead, client communication, and administrative time?
- Does it factor in risk contingency (a buffer percentage based on overall uncertainty)?
- These decisions affect the output format and what each twin needs to consider

### 9.6 Methodology Evolution

- Your AI SDLC framework will evolve over time — new phases might be added, existing phases might be restructured, AI capabilities will change
- The twin architecture needs to be extensible: adding a seventh twin, modifying an existing twin's evaluation dimensions, or adjusting the Orchestrator's cross-phase logic should not require rebuilding the system
- A2A Agent Cards make this extensibility concrete — a new twin registers its Agent Card (including `crossPhaseSubscriptions` and `crossPhasePublishes`), and the Orchestrator discovers it automatically. LangGraph's graph topology would still need a code change to add the new node and edges, but the communication protocol is handled by A2A without modification

### 9.7 A2A Cross-Phase Signal Latency Risk

- In the reactive parallel model (Section 1.3.3), downstream twins wait up to 15 seconds for upstream A2A signals before proceeding with defaults. If upstream twins consistently approach this timeout (due to complex projects or slow LLM responses), downstream twins may frequently operate without cross-phase signals, reducing the benefit of the reactive architecture
- Mitigation: monitor signal delivery latency via Langfuse traces. If a specific twin consistently times out, consider giving it a faster/smaller model, simplifying its evaluation dimensions, or increasing the timeout threshold for that twin's subscribers. Alternatively, move that twin to an earlier execution tier (a pre-superstep that runs before the main parallel fan-out)
- Worst case: if A2A signal exchange adds too much latency relative to its estimation accuracy benefit, the system can fall back to the simpler Pass 1 model (full parallel, no cross-phase signals) for both passes — the A2A layer is additive, not load-bearing

### 9.8 A2A Protocol Maturity and Dependency Risk

- A2A reached v1.0 under the Linux Foundation with 150+ member organizations, but it is still a young standard. Breaking changes in future versions, particularly to the Agent Card format or task lifecycle states, could require updates to the twin communication layer
- Mitigation: pin the A2A client library version (`langgraph-a2a-client`) and abstract A2A interactions behind an internal interface layer. If A2A introduces breaking changes, only the interface layer needs updating — twin logic and Orchestrator logic remain untouched
- LangGraph's native A2A support (via `langgraph-api>=0.4.21`) ties the project to LangChain's A2A implementation timeline — if LangChain falls behind the A2A spec, the project may need to maintain a fork or switch to the reference A2A Python client

### 9.9 LangGraph State Size and Checkpointing Overhead

- The `EstimationState` object grows across the two-pass cycle: raw input + Stage 2-3 data + six Pass 1 estimates + six gap lists + clarifying questions + user answers + six Pass 2 estimates + cross-phase signals. For complex projects with large raw inputs and detailed twin outputs, this could exceed 100KB+ per estimation run
- LangGraph checkpoints the full state at the `await_user_answers` interrupt and after every superstep — frequent large writes to Neo4j could add latency, especially since graph writes involve creating/updating multiple nodes and relationships per checkpoint
- Mitigation: use selective state serialization (only checkpoint keys that changed), compress large text fields before storage, and set a reasonable TTL on checkpoint data (e.g., 7 days for incomplete estimates, 90 days for completed ones). Consider batching checkpoint writes using Neo4j's `UNWIND` for bulk operations. For Qdrant, embedding generation is the bottleneck — defer indexing of new projects to an async background job rather than blocking the estimation flow

---

## 10. MVP Scoping

### 10.1 What to Build First

- **LangGraph Orchestrator + all six twins** running the two-pass cycle, implemented as a single StateGraph with static fan-out/fan-in (Section 1.4.2)
- **Pass 1: full parallel, no A2A cross-phase signals** — all six twins run independently in a single superstep, return estimates + gaps. This is the simpler execution model and validates the core estimation logic without the complexity of reactive signaling
- **A2A for Orchestrator ↔ twin communication only** — twins receive context and return results via A2A `message/send` with DataPart payloads. Each twin publishes an Agent Card. But peer-to-peer cross-phase signaling between twins is deferred to post-MVP
- **Pass 2: same parallel model as Pass 1** — enriched context distributed, twins run independently again. Cross-phase consistency checking runs in the `consistency_check` node after fan-in, but without real-time A2A signals between twins during execution
- **Human-in-the-loop via LangGraph interrupt** at the `await_user_answers` node, with Neo4j-backed checkpointing for state persistence
- **Stage 1 input only** (unstructured text, no file uploads) plus a simplified Stage 2 (AI-extracted fields, confirmation only — no expanded NFR/commercial/team sections)
- Clarifying questions with defaults (Stage 4)
- Structured estimate output displayed in the UI (Stage 5 summary view)
- Manual export (copy/paste) for proposal insertion
- **Langfuse integration** for basic observability from day one — trace every twin execution and Orchestrator decision
- **Single LLM model** for all twins and Orchestrator (simplifies deployment, cost modeling, and debugging)

### 10.2 What to Add Next

- **A2A cross-phase signaling (reactive parallel model)** — enable peer-to-peer SSE signals between twins during Pass 2 as described in Section 1.3.3. This is the highest-value architectural upgrade post-MVP
- Full Stage 2 input (engagement model, technical environment, NFRs) and Stage 3 (AI maturity assessment, team profile)
- File upload parsing (RFPs, meeting notes) for Stage 1
- Historical data capture and basic calibration loop (Section 6) — Neo4j graph for estimate provenance, Qdrant indexing pipeline for new project embeddings
- Qdrant hybrid search integration in `parse_input` node for calibration context injection from similar past projects
- Project profile templates based on accumulated data (Neo4j community detection + Qdrant cluster analysis)
- Proposal template integration (structured export)
- Full reasoning chain visibility ("explain this estimate") powered by Langfuse trace queries via its API
- Selective twin re-estimation via conditional edges (consistency check triggers targeted re-runs)
- Multi-model strategy: faster models for simpler twins, more capable models for Development Architect and Orchestrator

### 10.3 What to Defer

- Multi-user collaboration on estimates
- Automated proposal document generation
- Integration with project management tools for actuals tracking
- Advanced analytics on estimation accuracy trends across the portfolio
- External A2A interoperability (connecting to third-party agents outside the system)
- LangGraph Cloud deployment (start with self-hosted Docker containers)
