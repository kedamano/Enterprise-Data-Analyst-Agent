import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function Markdown({ children }: { children: string }) {
  return (
    <div className="da-markdown text-[14.5px] leading-[1.8] text-slate-700">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ ...p }) => (
            <h1
              className="mt-5 mb-2 text-xl font-semibold text-slate-900"
              {...p}
            />
          ),
          h2: ({ ...p }) => (
            <h2
              className="mt-5 mb-2 text-lg font-semibold text-slate-900 border-b border-slate-200 pb-1"
              {...p}
            />
          ),
          h3: ({ ...p }) => (
            <h3
              className="mt-4 mb-1.5 text-base font-semibold text-slate-800"
              {...p}
            />
          ),
          p: ({ ...p }) => <p className="my-2.5" {...p} />,
          ul: ({ ...p }) => (
            <ul className="my-2.5 list-disc pl-5 space-y-1" {...p} />
          ),
          ol: ({ ...p }) => (
            <ol className="my-2.5 list-decimal pl-5 space-y-1" {...p} />
          ),
          li: ({ ...p }) => <li className="pl-1" {...p} />,
          a: ({ ...p }) => (
            <a
              className="text-indigo-600 underline underline-offset-2 hover:text-indigo-700"
              target="_blank"
              rel="noopener noreferrer"
              {...p}
            />
          ),
          strong: ({ ...p }) => (
            <strong className="font-semibold text-slate-900" {...p} />
          ),
          blockquote: ({ ...p }) => (
            <blockquote
              className="my-3 border-l-2 border-indigo-300 pl-3 text-slate-500 italic"
              {...p}
            />
          ),
          code: ({ className, children, ...props }) => {
            const isBlock = /language-/.test(className || "");
            if (isBlock) {
              return (
                <code
                  className="block rounded-lg bg-slate-900 border border-slate-700 p-3 my-3 overflow-x-auto text-[13px] text-emerald-300 font-mono"
                  {...props}
                >
                  {children}
                </code>
              );
            }
            return (
              <code
                className="rounded bg-indigo-50 px-1.5 py-0.5 text-[13px] text-indigo-700 font-mono"
                {...props}
              >
                {children}
              </code>
            );
          },
          pre: ({ ...p }) => (
            <pre className="bg-transparent p-0 my-0" {...p} />
          ),
          table: ({ ...p }) => (
            <div className="my-3 overflow-x-auto rounded-lg border border-slate-200">
              <table
                className="w-full border-collapse text-[13px]"
                {...p}
              />
            </div>
          ),
          th: ({ ...p }) => (
            <th
              className="border-b border-slate-200 bg-slate-50 px-3 py-2 text-left font-semibold text-slate-800"
              {...p}
            />
          ),
          td: ({ ...p }) => (
            <td
              className="border-b border-slate-100 px-3 py-2 text-slate-700"
              {...p}
            />
          ),
          hr: ({ ...p }) => <hr className="my-4 border-slate-200" {...p} />,
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
}
