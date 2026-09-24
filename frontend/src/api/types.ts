// Mirrors the backend read models (backend/src/accrueboard/services/queries.py).
// Money arrives as decimal strings and is never converted to floating point for arithmetic.

export type Money = string

export type TaskState =
  | 'queued'
  | 'processing'
  | 'auto_approved'
  | 'needs_review'
  | 'approved'
  | 'blocked'
  | 'rejected'
  | 'failed'
  | 'posted'

export type AlertLevel = 'warning' | 'breach'

export interface ClientSummary {
  id: string
  name: string
  business: string
  auto_post_threshold: number
}

export interface UserSummary {
  id: string
  name: string
  role: string
}

export interface AccountSummary {
  code: string
  name: string
  type: string
  description: string
  role: string | null
}

// An unpaid invoice waiting on a person, close to or past its due date.
export interface DueAlert {
  task_id: string
  state: TaskState
  level: AlertLevel
  due_date: string
  days_left: number
  message: string
}

export interface Card {
  task_id: string
  state: TaskState
  state_entered_at: string
  age_seconds: number
  alert: AlertLevel | null
  received_at: string
  filename: string
  doc_type: string | null
  vendor: string | null
  document_number: string | null
  total: Money | null
  score: number | null
  rules: string[]
  summary: string | null
  assignee_id: string | null
  due: DueAlert | null
}

export interface LineItem {
  description: string
  quantity: string
  unit_price: Money
  amount: Money
  taxable: boolean
}

export interface ExtractedDocument {
  doc_type: string
  vendor_name: string | null
  vendor_state: string | null
  document_number: string | null
  issue_date: string | null
  due_date: string | null
  po_number: string | null
  lines: LineItem[]
  subtotal: Money | null
  discount: Money
  shipping: Money
  tax_rate: string | null
  tax: Money
  total: Money | null
  payment_method: string | null
  referenced_document_number: string | null
}

export interface FieldCheck {
  field: string
  grounded: boolean | null
  agreed: boolean | null
  confidence: number
}

export interface Extraction {
  confidence: number
  fields: FieldCheck[]
  second_pass: boolean
  second_pass_reason: string | null
  ungrounded: string[]
  unparsable: string[]
}

export interface LineCoding {
  line_index: number
  account: string
  reason: string
  llm_account: string | null
  classifier_account: string | null
  classifier_probability: number | null
  vendor_rule_account: string | null
  neighbour_account: string | null
  neighbour_share: number
  confidence: number
}

export interface Coding {
  accounts: string[]
  lines: LineCoding[]
  confidence: number
  vendor_known: boolean
  vendor_history: Record<string, number>
}

export interface RuleHit {
  rule: string
  severity: 'hard' | 'soft'
  detail: string
}

export interface Routing {
  outcome: 'auto_post' | 'needs_review'
  hits: RuleHit[]
  score: number
  threshold: number
  extraction_confidence: number
  coding_confidence: number
  factors: Record<string, number>
  summary: string
}

export interface AuditItem {
  seq: number
  occurred_at: string
  actor: string
  actor_kind: 'machine' | 'human'
  action: string
  from_state: string | null
  to_state: string | null
  details: Record<string, unknown>
  hash: string
}

export interface EntryLine {
  account_code: string
  account_name: string
  debit: Money
  credit: Money
}

export interface EntryView {
  id: string
  task_id: string | null
  entry_date: string
  memo: string
  reverses: string | null
  posted_at: string
  lines: EntryLine[]
}

export interface CallView {
  purpose: string
  model: string
  prompt_version: string
  cost_usd: string
  latency_ms: number
  replayed: boolean
}

export type SuggestedAction = 'approve' | 'reject' | 'block'
export type Verdict = 'confirmed' | 'false_positive' | 'uncertain'

export interface Suggestion {
  action: SuggestedAction
  summary: string
  question: string | null
  rule_assessments: { rule: string; verdict: Verdict; reason: string }[]
  lines: { line: number; account: string; reason: string }[]
  evidence: { source: string; detail: string; document_id: string | null }[]
}

export interface AssistantStep {
  turn: number
  tool: string
  input: Record<string, unknown>
  output: string
  is_error: boolean
}

// The review assistant's latest investigation (suggest-only; see agents/review_assistant).
export interface AssistantRun {
  status: 'done' | 'failed'
  suggestion: Suggestion | null
  error: string | null
  proposed_accounts: string[]
  steps: AssistantStep[]
  notes: string[]
  model: string
  prompt_version: string
  model_turns: number
  cost_usd: string
  replayed: boolean
  ran_at: string
}

export interface TaskDetail {
  card: Card
  client_id: string
  media_type: string | null
  has_file: boolean
  extracted: ExtractedDocument | null
  extraction: Extraction | null
  coding: Coding | null
  routing: Routing | null
  line_accounts: string[] | null
  assistant: AssistantRun | null
  last_error: string | null
  attempts: number
  audit: AuditItem[]
  audit_intact: boolean
  entries: EntryView[]
  calls: CallView[]
  cost_usd: string
}

export interface AgeAlert {
  task_id: string
  state: TaskState
  level: AlertLevel
  age: string
  limit: string
  message: string
}

export interface Bottlenecks {
  now: string
  alerts: AgeAlert[]
  due: DueAlert[]
  congestion: { state: string; count: number; capacity: number; message: string } | null
  counts: Record<string, number>
}

export interface LedgerView {
  entries: EntryView[]
  trial_balance: { account_code: string; account_name: string; balance: Money }[]
  balanced: boolean
}

export interface KnowledgeItem {
  id: string
  vendor_name: string
  description: string
  amount: Money
  account: string
  source: 'history' | 'confirmed' | 'corrected'
  document_ref: string | null
  created_at: string
}

export interface Stats {
  counts: Record<string, number>
  processed: number
  auto_posted: number
  human_reviewed: number
  automation_rate: number | null
  llm_cost_usd: string
  llm_calls: number
  replayed_calls: number
}

export interface ClockView {
  now: string
  offset_hours: number
  demo_mode: boolean
}

export interface ActionResult {
  task_id: string
  state: TaskState
  journal_entry: string | null
  knowledge_entries: number
}
