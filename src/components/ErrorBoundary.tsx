import { Component, type ErrorInfo, type ReactNode } from "react";

interface Props {
  children: ReactNode;
}

interface State {
  message: string | null;
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { message: null };

  static getDerivedStateFromError(error: Error): State {
    return { message: error.message };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("UI error", error, info.componentStack);
  }

  render() {
    if (this.state.message) {
      return (
        <div className="state-panel state-error" role="alert">
          <div>
            <strong>The interface hit an error</strong>
            <p>{this.state.message}</p>
          </div>
          <button className="button button-quiet" type="button" onClick={() => this.setState({ message: null })}>
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
