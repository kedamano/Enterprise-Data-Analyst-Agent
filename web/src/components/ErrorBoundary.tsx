import { Component, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

/**
 * React ErrorBoundary：捕获组件树内任意未捕获异常，防止整页白屏。
 * 提供"重试"按钮让用户恢复，同时把错误信息展示出来（开发环境排障）。
 */
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null });
  };

  render() {
    if (this.state.hasError) {
      return (
        <div
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            height: "100vh",
            padding: "2rem",
            fontFamily: "var(--font-sans, system-ui, sans-serif)",
            background: "hsl(var(--background))",
            color: "hsl(var(--foreground))",
          }}
        >
          <div
            style={{
              maxWidth: "520px",
              textAlign: "center",
              padding: "2rem",
              borderRadius: "1rem",
              border: "1px solid hsl(var(--border))",
              background: "hsl(var(--card))",
            }}
          >
            <div
              style={{
                fontSize: "3rem",
                marginBottom: "1rem",
                lineHeight: 1,
              }}
            >
              ⚠️
            </div>
            <h2
              style={{
                fontSize: "1.25rem",
                fontWeight: 600,
                margin: "0 0 0.5rem",
              }}
            >
              页面出了点问题
            </h2>
            <p
              style={{
                fontSize: "0.875rem",
                color: "hsl(var(--muted-foreground))",
                margin: "0 0 1.5rem",
                lineHeight: 1.6,
              }}
            >
              您可以点击下方按钮重试。如果问题持续存在，请刷新页面或联系管理员。
            </p>
            {this.state.error && (
              <pre
                style={{
                  textAlign: "left",
                  fontSize: "0.75rem",
                  background: "hsl(var(--muted) / 0.3)",
                  padding: "0.75rem",
                  borderRadius: "0.5rem",
                  overflow: "auto",
                  maxHeight: "120px",
                  marginBottom: "1rem",
                  color: "hsl(var(--destructive))",
                  border: "1px solid hsl(var(--destructive) / 0.2)",
                }}
              >
                {this.state.error.message}
              </pre>
            )}
            <button
              onClick={this.handleReset}
              style={{
                padding: "0.5rem 1.25rem",
                fontSize: "0.875rem",
                fontWeight: 500,
                borderRadius: "0.5rem",
                border: "none",
                cursor: "pointer",
                background: "hsl(var(--primary))",
                color: "hsl(var(--primary-foreground))",
              }}
            >
              重试
            </button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
