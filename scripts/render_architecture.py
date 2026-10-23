#!/usr/bin/env python3
"""Regenerate self-contained architecture SVGs using the vendored brand icons.

No network calls or third-party Python packages. See docs/diagrams/README.md
for PNG export and icon provenance. Layout is intentionally explicit so reviews
can track architecture changes without a diagram service or GUI dependency.
"""
from __future__ import annotations

import base64
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs' / 'diagrams'
ICON = OUT / 'icons'
INK = '#172b43'
MUTED = '#50657c'
TEAL = '#087e8b'
PURPLE = '#8050ba'
GREY = '#748398'


class Diagram:
    def __init__(self, width: int, height: int, title: str, description: str):
        self.parts = [f'''<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title><desc id="desc">{escape(description)}</desc>
<defs>''']
        for name, color in [('audio', TEAL), ('async', PURPLE), ('control', GREY)]:
            self.parts.append(f'<marker id="{name}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="{color}"/></marker>')
        self.parts.append('</defs>')
        self.rect(0, 0, width, height, '#f4f7fb', '#f4f7fb', 0)

    def rect(self, x, y, w, h, fill='white', stroke='#d6e0eb', radius=14, dash=None):
        self.parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="1.5"' + (f' stroke-dasharray="{dash}"' if dash else '') + '/>')

    def text(self, x, y, content, size=18, color=INK, weight=400, anchor='start'):
        self.parts.append(f'<text x="{x}" y="{y}" font-family="Arial, Helvetica, sans-serif" font-size="{size}" fill="{color}" font-weight="{weight}" text-anchor="{anchor}">{escape(content)}</text>')

    def lines(self, x, y, lines, size=18, color=MUTED, gap=27):
        for i, line in enumerate(lines):
            self.text(x, y + i * gap, line, size, color)

    def icon(self, name, x, y, size=52):
        data = base64.b64encode((ICON / f'{name}.svg').read_bytes()).decode()
        self.parts.append(f'<image x="{x}" y="{y}" width="{size}" height="{size}" xlink:href="data:image/svg+xml;base64,{data}"/>')

    def arrow(self, points, kind='audio', both=False):
        color = {'audio': TEAL, 'async': PURPLE, 'control': GREY}[kind]
        dash = '' if kind == 'audio' else ' stroke-dasharray="8 6"'
        start = f' marker-start="url(#{kind})"' if both else ''
        self.parts.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.6" stroke-linejoin="round"{dash}{start} marker-end="url(#{kind})"/>')

    def card(self, x, y, w, h, icon, title, lines, tint='white', size=19):
        self.rect(x, y, w, h, tint)
        self.icon(icon, x+18, y+18, 48)
        self.text(x+80, y+47, title, size, weight=700)
        self.lines(x+20, y+92, lines, 18)

    def save(self, name):
        (OUT / name).write_text('\n'.join(self.parts) + '\n</svg>\n')


def architecture():
    d = Diagram(2100, 1460, 'Twilio + Amazon Nova Sonic 2.5 architecture',
        'Current runtime architecture: Twilio Media Streams traverse optional CloudFront and ALB to FastAPI on ECS Fargate. Strands owns an identity-first Nova conversation and concurrent tools. A call lifecycle controller coordinates tool barriers, a Nova-only farewell, playback acknowledgement and bounded Twilio termination. Only confirmed termination admits a fixed post-call workflow in a dedicated two-worker executor: regional Sonic token costing, then Luna intelligence and optional AgentCore date resolution. Workers share the application process and are not durable jobs. DynamoDB, secret injection and observability are shown.')
    d.text(44, 61, 'Twilio + Amazon Nova Sonic 2.5', 36, weight=700)
    d.text(46, 99, 'Outbound collections voice agent  /  runtime and data flow', 22, MUTED)
    d.text(2050, 57, 'RUNTIME ARCHITECTURE', 15, MUTED, 700, 'end')
    d.text(2050, 84, 'Nova Sonic 2.5 · Strands Bidi', 17, MUTED, anchor='end')
    d.arrow('48,143 102,143'); d.text(116, 149, 'Bidirectional audio', 17)
    d.arrow('351,143 405,143', 'control'); d.text(419,149,'HTTPS / control / platform',17)
    d.arrow('750,143 804,143','async'); d.text(818,149,'Post-call work',17)
    d.text(1130,149,'Illustrated ingress: EnableCloudFront=true, UseVpcOrigin=true',17,MUTED)
    d.rect(342,190,1715,1187,'#fff','#b8c8d9',18)
    d.icon('aws',365,203,50); d.text(430,235,'AWS Cloud',23,weight=700)
    d.text(660,218,'Regional resources use AWS_REGION (application default: us-east-1)',17,MUTED)
    d.rect(650,250,805,865,'#fafcff','#af9bce',14)
    d.icon('vpc',668,262,35); d.text(715,288,'VPC · private subnets · NAT gateway for outbound access',18,'#644590',700)
    d.text(46,209,'EXTERNAL',15,MUTED,700)
    d.rect(45,231,235,63)
    d.text(162,262,'Caller / PSTN',21,weight=700,anchor='middle')
    d.text(162,288,'Phone audio + SMS',16,MUTED,anchor='middle')
    d.arrow('163,296 163,328',both=True)
    d.card(45,330,235,185,'twilio','Twilio',['Voice + Media Streams','G.711 μ-law · 8 kHz','Voice REST + SMS APIs'])
    d.card(382,330,220,185,'cloudfront','CloudFront',['Viewer TLS / WSS','Caching disabled','AllViewer headers'],size=18)
    d.card(700,330,220,185,'alb','ALB',['Internal · VPC origin','HTTP :80 → WS upgrade','Idle timeout: 120 s'])
    d.rect(980,310,425,755,'#fff','#e9a867',14)
    d.icon('ecs',997,324,48); d.icon('fargate',1050,324,48)
    d.text(1112,343,'Amazon ECS',23,weight=700)
    d.text(1112,371,'AWS Fargate · 1 task by default',17,MUTED)
    d.icon('fastapi',1005,391,29); d.icon('python',1045,391,29)
    d.text(1088,414,'FastAPI + Uvicorn · Python 3.12',17,weight=700)
    d.rect(1003,439,379,112,'#effaf9','#bddfdf',10)
    d.icon('strands',1018,453,32)
    d.text(1064,477,'Strands BidiAgent',22,weight=700)
    d.lines(1019,508,['SonicSession · aws-sdk-bedrock-runtime','PCM in: 16 kHz  /  PCM out: 24 kHz'],16,gap=25)
    d.rect(1003,573,379,174,'#f6f8fc','#d6e0eb',10)
    d.text(1020,602,'CallLifecycleController',21,weight=700)
    d.lines(1020,631,['end_conversation → begin_farewell','Tool barriers → localized Nova farewell','Response + audio IDs → playback ACK','Bounded hangup → final persistence'],16,gap=26)
    d.rect(1003,773,379,262,'#faf5ff','#d9c6ed',10)
    d.text(1020,804,'Post-call · disconnected',21,PURPLE,700)
    d.lines(1020,841,['1. Price + save regional Sonic cost','2. Call details → Luna extraction','3. Parser → AgentCore on a miss','4. Ready result → SMS / dispute flag','5. Save intelligence + phase status'],17,gap=27)
    d.text(1020,981,'Dedicated 2-worker post-call executor',16,INK,700)
    d.text(1020,1010,'Same process · no durable job queue',15,MUTED)
    d.arrow('1193,749 1193,771','async')
    d.card(1580,330,420,222,'bedrock','Amazon Bedrock',['Nova Sonic 2.5 · Tiffany polyglot','English · French · Spanish','Identity-first speech + Nova farewell','InvokeModelWithBidirectionalStream','IAM task role / SigV4'])
    d.card(1580,569,420,142,'dynamodb','Amazon DynamoDB',['DuesTable: account_id','CallsTable: call_sid · transcript + analytics'])
    d.card(1580,756,420,140,'bedrock','Bedrock Converse',['OpenAI GPT-6 Luna · US profile','Intelligence + fallback Python authoring'],'#fcf9ff')
    d.icon('openai',1938,774,38)
    d.card(1580,933,420,132,'agentcore','Bedrock AgentCore',['Code Interpreter · execute Python','aws.codeinterpreter.v1 · date fallback'],'#fcf9ff')
    d.arrow('282,419 380,419',both=True); d.text(331,394,'WSS',16,TEAL,anchor='middle')
    d.arrow('604,419 698,419',both=True); d.text(651,394,'HTTP :80',15,TEAL,anchor='middle')
    d.arrow('922,489 1001,489',both=True); d.text(960,472,':8080',15,TEAL,anchor='middle')
    d.arrow('1384,485 1485,485 1485,441 1578,441',both=True)
    d.text(1490,408,'Audio stream',16,TEAL,anchor='middle')
    d.arrow('1407,626 1578,626','control',True); d.text(1490,605,'Read / write',16,MUTED,anchor='middle')
    d.arrow('1384,821 1578,821','async',True); d.text(1480,801,'Converse',16,PURPLE,anchor='middle')
    d.arrow('1384,990 1578,990','async',True); d.text(1490,970,'Fallback only',16,PURPLE,anchor='middle')
    d.arrow('978,589 164,589 164,517','control')
    d.text(368,570,'App → Twilio REST: create / end call, Call details, SMS',17,MUTED)
    d.card(45,697,235,187,'react','Operator UI',['React · shadcn/ui','Vite · Tailwind · Lucide','REST + analytics polling'])
    d.arrow('282,784 319,784 319,495 380,495','control')
    d.text(44,915,'Full-width due and nested call tables.',15,MUTED)
    d.text(44,940,'Call now → POST /api/calls',15,MUTED)
    d.text(44,978,'The current API has no',16,MUTED)
    d.text(44,1003,'application authentication.',16,MUTED)
    d.text(44,1045,'Twilio signature validation',16,MUTED)
    d.text(44,1070,'is not implemented.',16,MUTED)
    d.rect(682,710,259,300,'#f2f5fa','#d6e0eb',10)
    d.text(702,742,'NETWORK OPTIONS',16,MUTED,700)
    d.lines(702,782,['Shown: VPC origin','Internal ALB + private','tasks; outbound HTTPS','uses the NAT gateway.','','Other template options:','Public CloudFront origin','or direct ALB with TLS.'],17,gap=27)
    d.arrow('530,1198 530,1138 1015,1138 1015,1067','control')
    d.text(576,1130,'Execution role injects env at task start',16,MUTED)
    d.arrow('1193,1067 1193,1198','control')
    d.text(1210,1155,'stdout / awslogs',16,MUTED)
    d.card(382,1200,355,139,'secrets','Secrets Manager',['Existing secret ARNs → ECS env','SSM parameter ARNs also supported'],size=20)
    d.card(871,1200,458,139,'cloudwatch','Amazon CloudWatch',['JSON call events + OpenTelemetry console spans','Logs Insights · two dashboards'],size=21)
    d.icon('opentelemetry',1280,1218,27)
    d.card(1490,1200,510,139,'cloudtrail','CloudTrail → Amazon S3',['AWS management events → encrypted S3 bucket','Audit trail is separate from call analytics'],size=21)
    d.icon('s3',1936,1218,38)
    d.text(45,1420,'Source: app/ + infra/template.yaml  ·  See docs/architecture.md for component links, ingress conditions and execution limits.',17,MUTED)
    d.save('architecture.svg')


def call_processing():
    d = Diagram(2100, 1230, 'Call execution and post-call workflow',
        'One Fargate application process handles live Nova audio and native asynchronous tools. Public provider usage deltas are accumulated once without pricing calls. A single controller verifies farewell playback, performs bounded SDK and telephone cleanup, closes the WebSocket, drains writes and saves analytics. Both the launcher and post-call worker require confirmed telephone completion. A retained workflow saves regional Sonic cost before Twilio Call lookup, Luna extraction and optional AgentCore date resolution. Validated ready intelligence gates follow-up actions; intelligence is saved separately. Workers are process-local, with a dedicated two-thread executor and no durable queue.')
    d.text(45,61,'Call execution and post-call workflow',35,weight=700)
    d.text(46,100,'One ECS Fargate application process · live audio, termination, and retained post-call work',22,MUTED)
    d.text(2055,59,'EXECUTION BOUNDARIES',15,MUTED,700,'end')

    d.rect(45,150,2010,280,'#effaf9','#bddfdf',14)
    d.text(70,190,'1  WHILE CONNECTED',20,TEAL,700)
    d.text(2030,190,'Strands owns connection recovery and scheduled renewal (420 s default)',18,MUTED,anchor='end')
    d.card(70,225,600,154,'strands','Strands BidiAgent + Nova Sonic 2.5',[
        'Twilio audio ↔ Nova speech in the existing session',
        'Brief AI introduction → confirm intended person',
        'English, French or Spanish · Nova-only speech'],size=22)
    d.card(750,225,600,154,'dynamodb','Native asynchronous tools',[
        'update_due_record · capture_payment_date_phrase',
        'end_conversation · begin_farewell',
        'Ordered live writes await actual store completion'],size=22)
    d.card(1430,225,600,154,'bedrock','Call-scoped usage accumulator',[
        'Text + audio input/output deltas counted once',
        'Call model + AWS region retained across restarts',
        'No pricing lookup or extra model request'],size=22)
    d.arrow('672,299 748,299','control',True)
    d.arrow('370,381 370,407 1730,407 1730,381','control')
    d.text(1050,401,'Public receive() captures usage before output fences',17,MUTED,anchor='middle')

    d.rect(45,460,2010,254,'#f6f8fc','#c2cfde',14)
    d.text(70,500,'2  ONE TELEPHONE LIFECYCLE CONTROLLER',20,INK,700)
    d.text(2030,500,'Caller disconnect and SDK failure also converge on cleanup',18,MUTED,anchor='end')
    d.card(70,535,600,152,'strands','Closing handshake',[
        'Settle tools / writes → begin_farewell readiness',
        'Nova farewell: transcript + audio + response end',
        'Matching Twilio playback mark acknowledged'],size=22)
    d.card(750,535,600,152,'twilio','Bounded telephone cleanup',[
        'Close admission → stop SDK → Twilio hangup',
        'Confirm terminal Call status; bounded retry',
        'Close WebSocket; preserve pending write workers'],size=22)
    d.card(1430,535,600,152,'dynamodb','Finalize and save call analytics',[
        'Drain admitted writes → save finalized analytics',
        'Persist usage, model, region and termination proof',
        'Launcher + workflow each verify disconnect proof'],size=22)
    d.arrow('672,608 748,608','control')
    d.arrow('1352,608 1428,608','control')
    d.arrow('1730,689 1730,755 295,755 295,793','async')
    d.text(1010,748,'Confirmed telephone completion + saved analytics admit post-call work',19,PURPLE,anchor='middle')

    d.rect(45,795,2010,320,'#faf5ff','#d9c6ed',14)
    d.text(70,831,'3  RETAINED POST-CALL WORKFLOW',20,PURPLE,700)
    d.text(2030,831,'Separate executor: at most 2 blocking operations at once across calls',18,MUTED,anchor='end')
    d.card(70,857,450,181,'python','Calculate + save Sonic cost',[
        'Decimal × regional AWS rate snapshot',
        'Persist sonic_cost before intelligence',
        'Partial coverage → observed subtotal',
        'No runtime pricing API or model call'],tint='#fff',size=21)
    d.card(610,857,675,181,'bedrock','Luna intelligence + date resolution',[
        'Twilio Call details → Bedrock Converse (GPT-6 Luna)',
        'Validate extraction + at most one repair request',
        'Date parser → AgentCore Python only on a miss',
        'ready + current call/phrase gate the due-date write'],size=22)
    d.icon('openai',1228,873,35)
    d.card(1375,857,655,181,'dynamodb','Eligible actions + intelligence save',[
        'ready result → payment SMS / dispute / wrong-number action',
        'Persist intelligence, action outcomes and phase status',
        'Dashboard polls stored call details and token costs',
        'Partial / failed extraction skips follow-up actions'],size=22)
    d.arrow('522,946 608,946','async')
    d.arrow('1287,946 1373,946','async')
    d.text(1050,1084,'Cost failure does not suppress extraction; intelligence failure does not erase saved cost.',19,PURPLE,anchor='middle')
    d.text(45,1154,'Unconfirmed telephone completion: save unresolved analytics; withhold pricing, intelligence and follow-up actions.',19,INK)
    d.text(45,1185,'Process-local tasks share CPU / memory. A 20 s shutdown grace period is not durable delivery or cross-process exactly-once execution.',18,MUTED)
    d.text(45,1215,'Source: sonic_bridge.py · sonic_usage.py · call_lifecycle.py · main.py · postcall_workflow.py · postcall.py',17,MUTED)
    d.save('call-processing.svg')


def deployment():
    d = Diagram(1900, 960, 'Deployment and SDK supply chain',
        'A reviewed Git commit is packaged into a ZIP with a source manifest and uploaded to a private versioned Amazon S3 bucket. The repository host is independent of the CodeBuild S3 source. CloudFormation pins the commit, manifest hash and S3 object version. CodeBuild verifies the package, builds the Docker image and pushes it to ECR. The reviewed release selects that image digest in CloudFormation, which updates ECS Fargate. An optional Lambda can trigger and await the build when ContainerImage is blank; it is not required by the digest-pinned release flow. A separate create-only Lambda seeds DynamoDB.')
    d.text(45,60,'Build, deploy and SDK supply chain',34,weight=700)
    d.text(46,99,'CodeBuild builds the reviewed source; CloudFormation deploys the selected ECR digest',21,MUTED)
    d.text(1855,56,'BUILD AND DEPLOYMENT',15,MUTED,anchor='end')
    d.rect(45,145,1810,538,'white','#b8c8d9',16)
    d.icon('gitlab',82,169,44)
    d.icon('github',136,169,44)
    d.text(197,188,'Reviewed Git commit · host-independent source',22,weight=700)
    d.text(197,219,'scripts/package_source.py → source archive + manifest',18,MUTED)
    d.arrow('225,229 225,268','control')
    d.text(845,189,'SOURCE PROVENANCE',15,MUTED,700)
    d.text(845,219,'Commit SHA + source version + manifest SHA-256 pinned in stack parameters',18,MUTED)
    d.card(80,270,290,178,'s3','Source package',[
        'Private, versioned S3 bucket','ZIP + full file manifest','Exact object version selected'],size=22)
    d.card(450,270,290,178,'codebuild','AWS CodeBuild',[
        'Verify source before build','Docker multi-stage build','Push commit-tagged image'],size=22)
    d.card(820,270,290,178,'ecr','Amazon ECR',[
        'Commit tag + content digest','Image scanning on push','Select verified image digest'],size=22)
    d.card(1190,270,290,178,'cloudformation','CloudFormation',[
        'infra/template.yaml','ContainerImage = ECR digest','Update ECS task definition'],size=20)
    d.card(1560,270,260,178,'ecs','ECS / Fargate',[
        'Pull selected image digest','Rolling service update','Dashboard + voice bridge'],size=21)
    for a,b in [(372,448),(742,818),(1112,1188),(1482,1558)]:
        d.arrow(f'{a},357 {b},357','control')
    d.card(80,505,790,166,'lambda','Optional in-stack build trigger',[
        'ContainerImage blank → Lambda starts and polls CodeBuild',
        'Returns build ID / status; the task uses the configured image tag',
        'With a pinned ContainerImage, the trigger skips the build'],tint='#f8f9fc',size=23)
    d.arrow('595,503 595,450','control')
    d.text(620,484,'Optional alternative',16,MUTED)
    d.rect(925,505,895,166,'#f5faf7','#b7d3c1',14)
    d.text(947,543,'Verified source → identifiable container',23,weight=700)
    d.lines(947,579,[
        'S3 version, manifest, complete file set, hashes and modes are checked.',
        'BUILD_REVISION + OCI revision label identify the verified Git commit.',
        'Execution/task roles supply secrets and scoped service permissions.'],18,gap=28)
    d.rect(45,717,830,190,'white','#d6e0eb',14)
    d.text(68,755,'Create-only seed resource',23,weight=700)
    d.icon('lambda',72,782,44)
    d.arrow('130,804 199,804','control')
    d.icon('dynamodb',214,782,44)
    d.text(285,800,'DuesTable · account_id',19,INK,700)
    d.text(285,829,'Two demo records on stack Create only',18,MUTED)
    d.text(68,878,'Separate from voice traffic and post-call workers.',18,MUTED)
    d.rect(903,717,952,190,'white','#d6e0eb',14)
    d.text(925,755,'Build tools and application SDKs',23,weight=700)
    for x,icon,label in [(925,'docker','Docker'),(1055,'react','React'),(1185,'vite','Vite'),(1315,'python','Python'),(1445,'fastapi','FastAPI'),(1575,'strands','Strands'),(1705,'twilio','Twilio')]:
        d.icon(icon,x+15,780,36);d.text(x+33,840,label,16,MUTED,anchor='middle')
    d.text(925,878,'Also: boto3 · aws-sdk-bedrock-runtime · bedrock-agentcore · OpenTelemetry',17,MUTED)
    d.text(45,940,'Source: scripts/package_source.py · infra/template.yaml · infra/buildspec.yml · Dockerfile · pyproject.toml',17,MUTED)
    d.save('deployment.svg')


if __name__ == '__main__':
    OUT.mkdir(parents=True, exist_ok=True)
    architecture()
    call_processing()
    deployment()
    print('Wrote docs/diagrams/{architecture,call-processing,deployment}.svg')
