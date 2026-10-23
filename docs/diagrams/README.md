# Architecture diagrams

These diagrams describe the current repository implementation. The runtime
figure uses the private CloudFront VPC-origin mode. The deployment figure shows
an exact Git commit packaged into a versioned S3 ZIP, verified by
CodeBuild, built into an ECR image and deployed to ECS by digest. The execution
figure shows the live-call and post-disconnect boundaries.

| Diagram | Scalable source | Shareable raster |
| --- | --- | --- |
| Runtime, audio, post-call processing and data flow | [architecture.svg](architecture.svg) | [architecture.png](architecture.png), 4200 × 2920 |
| Live-call, termination and post-call execution boundaries | [call-processing.svg](call-processing.svg) | [call-processing.png](call-processing.png), 4200 × 2460 |
| Build, deployment and SDK supply chain | [deployment.svg](deployment.svg) | [deployment.png](deployment.png), 3800 × 1920 |

Each SVG is self-contained: icons are embedded, there are no remote image
requests or scripts, and a title/description is included for accessibility.
The runtime figure illustrates `EnableCloudFront=true, UseVpcOrigin=true`;
the template defaults both flags to false. See the
[architecture guide](../architecture.md) for ingress modes, source links and
execution limits.

The execution figure follows the implemented gates: live usage accumulation,
confirmed telephone completion and analytics persistence, cost persistence,
then Luna intelligence and eligible actions. The two post-call workers share
the application process and do not represent a durable external job service.
The build figure shows digest selection through CloudFormation on the main
release path and the optional Lambda build trigger as a separate branch.
GitLab and GitHub marks identify repository-host options; the implemented
CodeBuild source is the verified S3 package, independent of the Git remote.

## Regenerate

The layout source is [scripts/render_architecture.py](../../scripts/render_architecture.py).
It uses Python's standard library and the checked-in icon files; rebuilding
SVGs requires no network, cloud credentials or diagram service.

```sh
python3 scripts/render_architecture.py
```

For the committed PNGs, use Node.js with `sharp` in
your tooling environment. This does not add a runtime application dependency:

```sh
node - <<'JS'
const sharp = require('sharp');
(async () => {
  for (const name of ['architecture', 'call-processing', 'deployment']) {
    await sharp(`docs/diagrams/${name}.svg`, {density: 144})
      .png().toFile(`docs/diagrams/${name}.png`);
  }
})();
JS
```

## Icon provenance and terms

[icons/manifest.json](icons/manifest.json) records each original URL, archive
member when applicable, retrieval date and SHA-256. Vendored icon files are
unmodified; diagrams embed them with their original aspect ratio. Monochrome
third-party marks come from Simple Icons. Product names remain text so the
meaning does not depend on color or logo recognition.

- **AWS:** official [AWS Architecture Icons](https://aws.amazon.com/architecture/icons/),
  release **31 July 2026**, downloaded from the archive linked by that page.
  The archive includes the current Amazon Bedrock and Bedrock AgentCore icons.
  AWS artwork is subject to AWS's architecture-icon usage terms and
  [AWS trademark guidelines](https://aws.amazon.com/trademark-guidelines/).
  The icons identify services; they do not imply AWS endorsement of this sample.
- **OpenAI, Twilio, React, FastAPI, Python, Docker, GitHub, GitLab, Vite and OpenTelemetry:**
  [Simple Icons 13.21.0](https://github.com/simple-icons/simple-icons/tree/13.21.0/icons),
  distributed under [CC0](https://github.com/simple-icons/simple-icons/blob/13.21.0/LICENSE.md).
  The represented brands and trademarks remain their owners' property and
  their individual brand guidelines still apply.
- **Strands Agents:** favicon from the [official project site](https://strandsagents.com/).
  It is used as the project identifier; see its exact source and hash in the
  manifest. No separate logo license is inferred from the SDK license.

Supporting SDKs without a separately sourced mark (`boto3`,
`aws-sdk-bedrock-runtime`, `bedrock-agentcore`) are named explicitly alongside
the relevant platform/service marks rather than assigned invented logos.
