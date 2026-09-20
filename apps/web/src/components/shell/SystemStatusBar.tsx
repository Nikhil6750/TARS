import React, { useEffect, useState } from 'react';
import { MonitorStatus, fetchMonitorStatus } from '../../services/monitors';
import { MonitorStrip } from '../voice/MonitorStrip';

/**
 * Market/system status (MT5, TradingView, calendar, news, live quote) belongs in the workspace,
 * not under the orb. Polls only while the workspace is mounted.
 */
export const SystemStatusBar: React.FC = () => {
  const [status, setStatus] = useState<MonitorStatus | null>(null);
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      const next = await fetchMonitorStatus();
      if (alive) setStatus(next);
    };
    void poll();
    const timer = window.setInterval(poll, 4000);
    return () => { alive = false; window.clearInterval(timer); };
  }, []);
  return (
    <div className="bg-[#0c0e14] px-4 pb-2 pt-1 border-b border-slate-800" aria-label="System status">
      <MonitorStrip status={status} alert={null} />
    </div>
  );
};
