/** Entry point for the Market X-Ray dashboard (served at /market.html). */
import React from 'react';
import ReactDOM from 'react-dom/client';
import { MarketXRayApp } from './MarketXRayApp';
import './styles.css';

const root = document.getElementById('market-root');
if (!root) {
  throw new Error('#market-root is missing from market.html');
}

ReactDOM.createRoot(root).render(
  <React.StrictMode>
    <MarketXRayApp />
  </React.StrictMode>
);
