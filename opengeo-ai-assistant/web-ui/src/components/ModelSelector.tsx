// Copyright (c) Microsoft Corporation.
// Licensed under the MIT license.

import React, { useState, useEffect, useRef } from 'react';
import './ModelSelector.css';
import { authenticatedFetch } from '../services/authHelper';
import { useT } from '../i18n/I18nContext';

interface ModelOption {
  id: string;
  name: string;
  isDefault?: boolean;
  isAvailable?: boolean;
}

interface ModelSelectorProps {
  onModelChange?: (modelId: string) => void;
  selectedModel?: string;
  apiBaseUrl?: string;
}

const DEFAULT_MODELS: ModelOption[] = [
  {
    id: 'unknown',
    name: 'Loading...',
    isDefault: true,
    isAvailable: false,
  },
];

const ModelSelector: React.FC<ModelSelectorProps> = ({ onModelChange, apiBaseUrl = '' }) => {
  const t = useT();
  const [isOpen, setIsOpen] = useState(false);
  const [models, setModels] = useState<ModelOption[]>(DEFAULT_MODELS);
  const [currentModel, setCurrentModel] = useState<string>('unknown');
  const dropdownRef = useRef<HTMLDivElement>(null);

  // Fetch the configured model from health endpoint
  useEffect(() => {
    const fetchAvailableModels = async () => {
      try {
        const response = await authenticatedFetch(`${apiBaseUrl}/api/health`);
        const data = await response.json();
        const llmInfo = data.checks?.llm_client || data.connectivity_tests?.llm_client;
        const llmStatus = llmInfo?.status;
        const isLlmOk = ['connected', 'configured', 'healthy', 'ok'].includes(
          llmStatus?.toLowerCase() || ''
        );

        if (isLlmOk && llmInfo?.model) {
          const modelId = llmInfo.model as string;
          const provider = llmInfo.provider as string | undefined;
          // Build a readable display name: show provider prefix if available
          const displayName = provider
            ? `${provider.charAt(0).toUpperCase() + provider.slice(1)} / ${modelId}`
            : modelId;

          const realModel: ModelOption = {
            id: modelId,
            name: displayName,
            isDefault: true,
            isAvailable: true,
          };
          setModels([realModel]);
          setCurrentModel(modelId);
          onModelChange?.(modelId);
        } else {
          setModels([{ id: 'unknown', name: 'Unavailable', isDefault: true, isAvailable: false }]);
        }
      } catch (err) {
        console.error('Failed to fetch model availability:', err);
      }
    };

    fetchAvailableModels();
    // Refresh every 30 seconds
    const interval = setInterval(fetchAvailableModels, 30000);
    return () => clearInterval(interval);
  }, [apiBaseUrl]);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const handleModelSelect = (modelId: string) => {
    const model = models.find((m) => m.id === modelId);
    // Only allow selecting available models
    if (model?.isAvailable) {
      setCurrentModel(modelId);
      setIsOpen(false);
      onModelChange?.(modelId);
    }
  };

  const currentModelInfo = models.find((m) => m.id === currentModel);

  return (
    <div className="model-selector" ref={dropdownRef}>
      <div
        className="model-selector-button"
        onClick={() => setIsOpen(!isOpen)}
        aria-expanded={isOpen}
        aria-haspopup="listbox"
        title={t('model.select')}
      >
        <span className="model-selector-label">{t('model.models')}</span>
      </div>

      {isOpen && (
        <div className="model-dropdown">
          <ul className="model-list" role="listbox">
            {models.map((model) => (
              <li
                key={model.id}
                className={`model-option ${currentModel === model.id ? 'selected' : ''} ${!model.isAvailable ? 'unavailable' : ''}`}
                onClick={() => handleModelSelect(model.id)}
                role="option"
                aria-selected={currentModel === model.id}
                aria-disabled={!model.isAvailable}
                style={{ opacity: model.isAvailable ? 1 : 0.5 }}
              >
                <span className="model-option-name">{model.name}</span>
                <span
                  className="model-availability-dot"
                  style={{
                    backgroundColor: model.isAvailable ? '#4CAF50' : '#F44336',
                    width: '6px',
                    height: '6px',
                    borderRadius: '50%',
                    marginLeft: '8px',
                    display: 'inline-block',
                  }}
                  title={model.isAvailable ? 'Available' : 'Not Deployed'}
                />
                {currentModel === model.id && <span className="check-mark">✓</span>}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
};

export default ModelSelector;
