import { Routes, Route, Navigate } from 'react-router-dom';
import { useState } from 'react';
import Landing from './pages/Landing/Landing';
import PatientIntake from './pages/PatientIntake/PatientIntake';
import PhysicianDashboard from './pages/PhysicianDashboard/PhysicianDashboard';
import StaffConsole from './pages/StaffConsole/StaffConsole';
import AdminPanel from './pages/AdminPanel/AdminPanel';

export type AppRole = 'patient' | 'physician' | 'nurse' | 'hospital_admin';

export default function App() {
  const [role, setRole] = useState<AppRole | null>(null);

  if (!role) {
    return <Landing onSelectRole={setRole} />;
  }

  return (
    <Routes>
      {role === 'patient' && (
        <>
          <Route path="/intake/*" element={<PatientIntake />} />
          <Route path="*" element={<Navigate to="/intake" replace />} />
        </>
      )}
      {role === 'physician' && (
        <>
          <Route path="/dashboard/*" element={<PhysicianDashboard />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </>
      )}
      {role === 'nurse' && (
        <>
          <Route path="/staff/*" element={<StaffConsole />} />
          <Route path="*" element={<Navigate to="/staff" replace />} />
        </>
      )}
      {role === 'hospital_admin' && (
        <>
          <Route path="/admin/*" element={<AdminPanel />} />
          <Route path="*" element={<Navigate to="/admin" replace />} />
        </>
      )}
      <Route path="*" element={<Landing onSelectRole={setRole} />} />
    </Routes>
  );
}
