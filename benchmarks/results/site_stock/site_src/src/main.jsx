import React from 'react';
import { createRoot } from 'react-dom/client';

// Locally bundled fonts (compiled into ./site/assets/ by Vite — zero external requests)
import '@fontsource-variable/space-grotesk';
import '@fontsource-variable/inter';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';
import '@fontsource/jetbrains-mono/700.css';

import './styles/app.css';
import { App } from './App.jsx';

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
