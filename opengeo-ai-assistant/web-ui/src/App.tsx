// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

import React, { useState } from 'react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import Header from './components/Header';
import MainApp from './components/MainApp';
import { GlobalStyles } from './styles/GlobalStyles';

const queryClient = new QueryClient();

export interface AppState {
  entryTarget: string | null;
  selectedDataset: any | null;
  chatMode: boolean;
  initialQuery?: string;
  sessionKey?: number;
}

function App() {
  const [appState, setAppState] = useState<AppState>({
    entryTarget: null,
    selectedDataset: null,
    chatMode: false,
    initialQuery: undefined,
    sessionKey: 1,
  });

  const [geointMode, setGeointMode] = useState<boolean>(false);
  const [selectedModel, setSelectedModel] = useState<string>(() => {
    return localStorage.getItem('earthcopilot-model') || 'gpt-5';
  });

  const handleModelChange = (modelId: string) => {
    setSelectedModel(modelId);
    localStorage.setItem('earthcopilot-model', modelId);
  };

  const handleRestartSession = () => {
    setAppState((prev) => ({
      ...prev,
      selectedDataset: null,
      chatMode: false,
      initialQuery: undefined,
      sessionKey: Date.now(),
    }));
    setGeointMode(false);
  };

  const handleDatasetSelect = (dataset: any) => {
    setAppState((prev) => ({ ...prev, selectedDataset: dataset, chatMode: true }));
  };

  return (
    <QueryClientProvider client={queryClient}>
      <GlobalStyles />
      <div className="app-container">
        <Header
          onReturnToLanding={handleRestartSession}
          onRestartSession={handleRestartSession}
          onModelChange={handleModelChange}
          selectedModel={selectedModel}
        />
        <MainApp
          appState={appState}
          onDatasetSelect={handleDatasetSelect}
          onReturnToLanding={handleRestartSession}
          onRestartSession={handleRestartSession}
          geointMode={geointMode}
          onGeointToggle={setGeointMode}
          selectedModel={selectedModel}
        />
      </div>
    </QueryClientProvider>
  );
}

export default App;
