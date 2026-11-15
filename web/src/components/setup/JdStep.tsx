"use client";

import { buttonPrimary, buttonSecondary, field, input } from "@/lib/styles";
import type { components } from "@/lib/api/schema";
import { savedJdLabel, type SavedJd } from "@/lib/setup";

type Props = {
  saved: SavedJd[] | null;
  notice: string | null;
  onPick: (text: string) => void;
  text: string;
  preview: components["schemas"]["JDParseOut"] | null;
  previewText: string | null;
  canContinue: boolean;
  onChange: (text: string) => void;
  onPreview: () => void;
  onBack: () => void;
  onNext: () => void;
};

const SAMPLE_JDS = [
  {
    label: "资深全栈工程师",
    text: `【岗位职责】
1. 负责核心业务系统的前后端全栈研发与架构演进，主导关键功能模块的设计与落地；
2. 参与高并发、高可用微服务架构建设，优化系统性能与响应延迟；
3. 负责 Web 端复杂交互与数据可视化开发，保障代码工程质量与测试覆盖率。

【任职要求】
1. 本科及以上学历，计算机相关专业，3年以上全栈或后端开发经验；
2. 精通 TypeScript、React / Next.js 与 Node.js / Go / Python 之一，有扎实的工程实践；
3. 熟悉 PostgreSQL / MySQL、Redis 等常用存储与缓存机制，具备 SQL 优化与索引调优能力；
4. 熟练掌握 Docker、CI/CD 自动化流水线，有微服务或云原生部署经验者优先；
5. 具备良好的架构思维、沟通协作能力和技术攻坚精神。`,
  },
  {
    label: "AI 产品 / 算法工程师",
    text: `【岗位职责】
1. 负责大语言模型（LLM）应用方案设计与落地，包括 Prompt 工程、RAG 检索增强及 Agent 智能体编排；
2. 构建高质量评测基准，持续追踪和评估模型的准确率、响应延迟与调用成本；
3. 协同产品与工程团队，将前沿 AI 能力转化为高价值的业务工具与交互体验。

【任职要求】
1. 计算机、人工智能或相关专业，熟练掌握 Python 编程与工程开发；
2. 熟悉主流 LLM API 及开源模型生态（LangChain、LlamaIndex 等），有 Agent 项目落地经验；
3. 了解向量数据库（Milvus、Chroma 等）与混合检索策略，具备评估与微调知识；
4. 具备清晰的逻辑思维、严谨的工程态度与快速自驱学习能力。`,
  },
];

export default function JdStep(props: Props) {
  const count = [...props.text].length;
  const valid = props.text.trim().length > 0 && count <= 10000;
  const stale = props.previewText !== null && props.previewText !== props.text;
  const hasSaved = Boolean(props.saved && props.saved.length > 0);

  return (
    <section className="flex min-w-0 flex-col gap-8" aria-labelledby="jd-title">
      <div>
        <h2 className="text-2xl text-ink" id="jd-title">
          目标职位
        </h2>
        <p className="mt-1 text-sm text-muted">
          {hasSaved
            ? "选一份之前解析过的，或者贴一份新的职位描述。"
            : "把职位描述整段贴进来，系统会从中整理出要考察的能力和权重。"}
        </p>
      </div>

      {hasSaved && (
        <ul className="border-t border-line" aria-label="解析过的职位描述">
          {props.saved?.map((jd) => {
            const label = savedJdLabel(jd);
            const selected = jd.text === props.text;
            return (
              <li key={jd.id} className="border-b border-line">
                <button
                  type="button"
                  aria-pressed={selected}
                  onClick={() => props.onPick(jd.text)}
                  className="group flex w-full min-w-0 cursor-pointer items-center gap-4 py-4 text-left"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-ink group-hover:text-accent-ink">{label.title}</span>
                    <span className="block truncate text-sm text-muted">{label.detail}</span>
                  </span>
                  <span
                    className={`shrink-0 text-sm ${selected ? "text-accent" : "text-faint group-hover:text-ink"}`}
                  >
                    {selected ? "已选" : "使用"}
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <div className="flex flex-col gap-3">
        <label className={field}>
          <span className="flex items-baseline justify-between">
            <span>职位描述</span>
            <span id="jd-count" className={count > 10000 ? "text-danger" : "text-faint"}>
              {count} / 10000{count > 10000 ? "，超出了" : ""}
            </span>
          </span>
          <textarea
            className={`${input} min-h-48 resize-y leading-relaxed`}
            rows={8}
            value={props.text}
            aria-describedby="jd-count"
            aria-invalid={count > 10000}
            onChange={(event) => props.onChange(event.target.value)}
            placeholder="职责、任职要求、技术栈……"
          />
        </label>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <button className={buttonPrimary} type="button" disabled={!valid} onClick={props.onPreview}>
            {props.preview ? "重新解析" : "解析职位"}
          </button>
          {SAMPLE_JDS.length > 0 && (
            <span className="flex flex-wrap items-baseline gap-x-3 text-sm text-faint">
              试试示例：
              {SAMPLE_JDS.map((sample) => (
                <button
                  key={sample.label}
                  type="button"
                  className="cursor-pointer text-muted underline-offset-4 hover:text-ink hover:underline"
                  onClick={() => props.onChange(sample.text)}
                >
                  {sample.label}
                </button>
              ))}
            </span>
          )}
        </div>
      </div>

      {props.notice && !stale && (
        <p className="border-l-2 border-accent pl-3 text-sm text-muted" role="status">
          {props.notice}
        </p>
      )}

      {stale && (
        <p className="border-l-2 border-warn pl-3 text-sm text-warn-ink" role="status">
          职位描述改过了，点“重新解析”更新下面的结果。
        </p>
      )}

      {props.preview && (
        <section className="flex min-w-0 flex-col gap-6 border-t border-line pt-8" aria-label="职位解析结果">
          <div>
            <h3 className="font-serif text-2xl text-ink">{props.preview.title || "未注明职位名称"}</h3>
            {props.preview.level && <p className="mt-1 text-sm text-muted">{props.preview.level}</p>}
          </div>

          <div className="flex flex-col">
            <h4 className="mb-2 text-sm text-muted">要考察的能力 · {props.preview.competencies.length} 项</h4>
            {props.preview.competencies.map((item, index) => (
              <article key={index} className="flex flex-col gap-1.5 border-t border-line py-4">
                <div className="flex items-baseline justify-between gap-3">
                  <h5 className="text-ink">{item.name}</h5>
                  <span className="shrink-0 font-serif text-ink">{Math.round(item.weight * 100)}%</span>
                </div>
                <p className="text-sm leading-relaxed text-muted">{item.description}</p>
                {item.probes.length > 0 && (
                  <ul className="mt-1 list-disc space-y-0.5 pl-5 text-sm text-ink-soft">
                    {item.probes.map((probe, i) => (
                      <li key={i}>{probe}</li>
                    ))}
                  </ul>
                )}
              </article>
            ))}
          </div>

          {props.preview.tech_terms.length > 0 && (
            <p className="border-t border-line pt-4 text-sm text-muted" aria-label="技术术语">
              <span className="mr-2">提到的技术</span>
              <span className="text-ink-soft">{props.preview.tech_terms.join(" · ")}</span>
            </p>
          )}
        </section>
      )}

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line pt-6">
        <button className={buttonSecondary} type="button" onClick={props.onBack}>
          上一步
        </button>
        <button className={buttonPrimary} type="button" disabled={!props.canContinue} onClick={props.onNext}>
          下一步：设置
        </button>
      </div>
    </section>
  );
}
