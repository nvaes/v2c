import React from "react";

// Erreur d'affichage (03/11/2026) : sans ce filet, une exception pendant le
// rendu démonte toute l'application et laisse une page blanche, sans rien
// pour comprendre. Le message et la pile restent affichés pour le support.
export default class ErrorBoundary extends React.Component<
  { children: React.ReactNode }, { error: Error | null }
> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    console.error("erreur d'affichage", error, info.componentStack);
  }

  render() {
    const { error } = this.state;
    if (!error) return this.props.children;
    return (
      <div style={{ padding: 24 }}>
        <h1>Erreur d'affichage</h1>
        <p className="error">{error.message}</p>
        <pre style={{ whiteSpace: "pre-wrap", fontSize: 12 }}>{error.stack}</pre>
        <button onClick={() => window.location.reload()}>Recharger la page</button>
      </div>
    );
  }
}
