# AWS Architecture

AR Collections Nova Sonic 2.5 Agent: an AI-initiated outbound collections voice
agent. Twilio carries the audio, Amazon Bedrock Nova Sonic 2.5 is the
speech-to-speech brain (Strands bi-di), DynamoDB persists state, and the
intelligence (disposition + structured data + next-best-action) is extracted
**asynchronously after the call ends** so it never adds to call latency.

Live app: https://d3k3puelhq4ike.cloudfront.net/

The numbered labels (1..10) in the diagram map one-to-one to the phase
explanations below it.

```mermaid
flowchart TB
    Debtor([Debtor phone<br/>verified number])

    subgraph Twilio["Twilio (telephony)"]
        T["(1) Outbound call +<br/>Media Streams WebSocket<br/>base64 mulaw 8k"]
    end

    subgraph AWS["AWS (us-east-1)"]
        CF["(2) Amazon CloudFront<br/>*.cloudfront.net wss TLS<br/>front door (managed cert)"]
        ALB["(3) ALB ingress<br/>locked to CloudFront prefix list<br/>pl-3b927c52 (HTTP:80)"]

        subgraph VPC["VPC vpc-0f002ebd48cdfc707 (public subnets)"]
            ECS["(4) ECS Fargate<br/>FastAPI bridge (app)"]
        end

        Sonic["(5) Strands BidiAgent &lt;-&gt;<br/>Bedrock Nova Sonic 2.5<br/>voice tiffany, bi-di stream<br/>mulaw&lt;-&gt;PCM transcode"]
        Tools["(6) In-call Strands tools<br/>update_due_record +<br/>capture_payment_date_phrase<br/>(captures phrase, no date math)"]
        CI["(6b) AgentCore Code Interpreter<br/>aws.codeinterpreter.v1<br/>writes+runs Python off-path<br/>resolves date phrase (~1.9s)"]

        subgraph DDB["(7) DynamoDB"]
            Dues[(DuesTable)]
            Calls[(CallsTable<br/>CallAnalytics)]
        end

        Extract["(8) ASYNC post-call intelligence<br/>Bedrock Converse text model<br/>amazon.nova-lite-v1:0<br/>disposition + structured data + NBA"]
        Triggers["(9) Downstream triggers<br/>PTP -&gt; Twilio SMS + pause<br/>Dispute -&gt; human escalation"]

        subgraph Obs["(10) Observability + secrets"]
            CW["CloudWatch Logs<br/>/ecs/ar-collections-nova-sonic/app<br/>+ dashboard + CloudTrail"]
            SM["Secrets Manager<br/>Twilio creds"]
        end
    end

    Debtor <-->|audio| T
    T <-->|wss| CF
    CF -->|http origin| ALB
    ALB --> ECS
    ECS <-->|real-time audio path| Sonic
    Sonic -.tool use.-> Tools
    Tools --> Dues
    ECS --> Calls

    Calls -. after call ends .-> Extract
    Extract -.resolve date phrase.-> CI
    Extract --> Triggers
    Triggers --> Dues
    Triggers -.->|SMS| T
    Extract --> Calls

    ECS --> CW
    ECS -. reads .- SM

    classDef async fill:#fde,stroke:#c39,stroke-width:2px;
    class Extract,Triggers,CI async;
```

## Phases

1. **Twilio outbound call.** Twilio places the AI-initiated outbound call (trial
   account, so only the verified number is reachable) and opens a Media Streams
   WebSocket carrying base64 G.711 mulaw, 8 kHz, mono.
2. **CloudFront TLS front door.** Amazon CloudFront (`*.cloudfront.net`,
   free managed cert) terminates viewer TLS at the edge so Twilio connects over
   `wss://`. Live: https://d3k3puelhq4ike.cloudfront.net/
3. **ALB ingress.** The Application Load Balancer accepts only CloudFront
   origin-facing edge IPs via managed prefix list `pl-3b927c52` on HTTP:80, so
   the raw ALB DNS is not directly reachable.
4. **ECS Fargate bridge.** The FastAPI app runs as a Fargate task in the public
   subnets of VPC `vpc-0f002ebd48cdfc707`, bridging the Twilio WebSocket to Nova.
5. **Strands BidiAgent + Nova Sonic 2.5.** Bi-directional streaming to Bedrock
   Nova Sonic 2.5 (voice `tiffany`) does STT + reasoning + TTS; the app
   transcodes mulaw 8k <-> PCM on the real-time audio path.
6. **In-call Strands tools.** `update_due_record` writes the outcome;
   `capture_payment_date_phrase` only RECORDS the debtor's raw date phrase
   (e.g. "next month 1st Monday") on the due record and returns immediately, so
   it adds no latency to the turn.
6b. **AgentCore Code Interpreter (off-path).** `app/code_interpreter.py` wraps
   the AWS Bedrock AgentCore Code Interpreter (`aws.codeinterpreter.v1`). After
   the call, a Bedrock TEXT model (the same `EXTRACTION_MODEL_ID`) is prompted
   with only the arguments (today's date + the captured phrase) to **author** a
   Python program on the fly; that model-written program is then `executeCode`-run
   in the sandbox to resolve a concrete `YYYY-MM-DD`. The ~1.9s session is why
   this runs only post-call,
   never on the real-time turn. The task role carries a dedicated
   least-privilege `agentcore-code-interpreter` grant
   (`bedrock-agentcore:StartCodeInterpreterSession` /
   `InvokeCodeInterpreter` / `StopCodeInterpreterSession`, scoped to
   `code-interpreter/*`) for exactly this SDK path - no
   Create/Delete/List, because the managed `aws.codeinterpreter.v1` default
   needs none.
7. **DynamoDB persistence.** `DuesTable` holds due/account records and
   `CallsTable` holds the CallAnalytics record (numeric fields stored as
   `Decimal` via float->Decimal conversion).
8. **ASYNC post-call intelligence.** After the call ends and off the audio path,
   the Bedrock Converse text model `amazon.nova-lite-v1:0` extracts the Call
   Disposition, Structured Conversation Data, and the Next-Best-Action from the
   transcript.
9. **Downstream triggers.** Driven by the disposition: Promise to Pay (PTP)
   sends a Twilio SMS payment link and pauses automated collection; Dispute
   Raised escalates the account to a human specialist (flag/event).
10. **Observability + secrets.** CloudWatch Logs
    (`/ecs/ar-collections-nova-sonic/app`), the
    `ar-collections-nova-sonic-observability` dashboard, and a CloudTrail trail
    record activity; Twilio credentials live in Secrets Manager.

## IAM (least privilege)

The ECS **task role** holds four separate, independently-scoped policy blocks so
each permission tracks exactly one capability (see `infra/template.yaml`):

- `bedrock-nova-sonic-bidi` - `bedrock:InvokeModelWithBidirectionalStream` /
  `InvokeModel` on just the Nova Sonic foundation-model ARN (phase 5).
- `bedrock-text-extraction` - `bedrock:InvokeModel` on just the configured
  `ExtractionModelId` text-model ARN (phase 8); the grant and the
  `EXTRACTION_MODEL_ID` env var derive from the same parameter so they cannot
  drift.
- `agentcore-code-interpreter` - the three AgentCore Code Interpreter session
  actions the off-path date tool uses (phase 6b), scoped to
  `arn:aws:bedrock-agentcore:<region>:<account>:code-interpreter/*`.
- `dynamodb-rw` / `cloudwatch-logs-write` - DynamoDB RW on the two tables and
  log-stream writes on the app log group.

`bedrock:InvokeModelWithBidirectionalStream` and the `bedrock-agentcore` session
actions are valid IAM actions not yet in cfn-lint's bundled catalog, so a scoped
`W3037` ignore lives in the TaskRole `Metadata` with an explanatory comment.

## Async / no-latency guarantee

Phases **8 and 9** (the code-interpreter resolution of relative dates, the LLM
structured extraction, and the downstream PTP/Dispute triggers) run
**asynchronously after the call has ended**, off the real-time audio/turn path,
as a fire-and-forget task. The call-latency metrics (`total_e2e_ms`,
`nova_total_ms`, `ttft_ms`) are computed on the audio path
(`session_start`..`session_end`) and are already frozen before this work runs,
so phases 8 and 9 **do not contribute to the call's end-to-end duration or
latency**.
