import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api, getToken, Role, setToken } from "./api";
import Login from "./pages/Login";
import EndpointsPage from "./pages/Endpoints";
import InventoryPage from "./pages/Inventory";
import PlansPage from "./pages/Plans";
import PlanTargetPage from "./pages/PlanTarget";
import MigrationPage from "./pages/Migration";
import LogsPage, { LogsIndexPage } from "./pages/Logs";
import UsersPage from "./pages/Users";
import PasswordPage from "./pages/Password";
import RunnerBadge from "./RunnerBadge";
import PlanReportPage from "./pages/PlanReport";

export default function App() {
  const [email, setEmail] = useState<string | null>(null);
  const [role, setRole] = useState<Role | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    if (!getToken()) {
      setChecked(true);
      return;
    }
    api
      .me()
      .then((u) => {
        setEmail(u.email);
        setRole(u.role);
      })
      .catch(() => setToken(null))
      .finally(() => setChecked(true));
  }, []);

  if (!checked) return null;

  if (!email) {
    return (
      <Login
        onLoggedIn={(e) => {
          setEmail(e);
          api.me().then((u) => setRole(u.role)).catch(() => setRole(null));
        }}
      />
    );
  }

  return (
    <>
      <header>
        <strong>V2FC</strong>
        <NavLink to="/endpoints" className={({ isActive }) => (isActive ? "active" : "")}>
          Environnements
        </NavLink>
        <NavLink to="/plans" className={({ isActive }) => (isActive ? "active" : "")}>
          Plans de migration
        </NavLink>
        <NavLink to="/migration" className={({ isActive }) => (isActive ? "active" : "")}>
          Migration
        </NavLink>
        <NavLink to="/logs" className={({ isActive }) => (isActive ? "active" : "")}>
          Logs
        </NavLink>
        {role === "admin" && (
          <NavLink to="/users" className={({ isActive }) => (isActive ? "active" : "")}>
            Utilisateurs
          </NavLink>
        )}
        <span style={{ marginLeft: "auto" }}>
          <RunnerBadge />
        </span>
        <span className="muted">
          <NavLink to="/password" style={{ color: "#cbd5e1" }}>{email}</NavLink>{" "}
          <button
            className="secondary"
            onClick={() => {
              setToken(null);
              setEmail(null);
              setRole(null);
            }}
          >
            Se déconnecter
          </button>
        </span>
      </header>
      <main>
        <Routes>
          <Route path="/" element={<Navigate to="/endpoints" replace />} />
          <Route path="/endpoints" element={<EndpointsPage />} />
          <Route path="/endpoints/:id/inventory" element={<InventoryPage />} />
          <Route path="/plans" element={<PlansPage />} />
          <Route path="/plans/:id/target" element={<PlanTargetPage />} />
          <Route path="/plans/:id/report" element={<PlanReportPage />} />
          <Route path="/migration" element={<MigrationPage />} />
          <Route path="/jobs/:id/logs" element={<LogsPage />} />
          <Route path="/logs" element={<LogsIndexPage />} />
          <Route path="/users" element={<UsersPage currentEmail={email} />} />
          <Route path="/password" element={<PasswordPage />} />
        </Routes>
      </main>
    </>
  );
}
