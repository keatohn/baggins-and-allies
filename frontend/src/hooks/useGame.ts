/**
 * Custom hook for managing game state with the backend API.
 */

import { useState, useCallback, useEffect } from 'react';
import api, {
  type ApiGameState,
  type ApiEvent,
  type Definitions,
  type AvailableActionsResponse,
} from '../services/api';

export interface UseGameReturn {
  // State
  gameId: string | null;
  gameState: ApiGameState | null;
  definitions: Definitions | null;
  availableActions: AvailableActionsResponse | null;
  isLoading: boolean;
  error: string | null;
  events: ApiEvent[];
  
  // Actions
  createGame: (gameId: string) => Promise<void>;
  loadGame: (gameId: string) => Promise<void>;
  purchase: (purchases: Record<string, number>) => Promise<void>;
  move: (from: string, to: string, unitIds: string[]) => Promise<void>;
  initiateCombat: (territoryId: string) => Promise<void>;
  continueCombat: () => Promise<void>;
  retreat: (retreatTo: string) => Promise<void>;
  mobilize: (destination: string, units: { unit_id: string; count: number }[]) => Promise<void>;
  endPhase: () => Promise<void>;
  endTurn: () => Promise<void>;
  clearError: () => void;
}

export function useGame(): UseGameReturn {
  const [gameId, setGameId] = useState<string | null>(null);
  const [gameState, setGameState] = useState<ApiGameState | null>(null);
  const [definitions, setDefinitions] = useState<Definitions | null>(null);
  const [availableActions, setAvailableActions] = useState<AvailableActionsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState<ApiEvent[]>([]);

  // Load definitions on mount
  useEffect(() => {
    api.getDefinitions()
      .then(setDefinitions)
      .catch(err => console.error('Failed to load definitions:', err));
  }, []);

  // Refresh available actions when game state changes
  const refreshAvailableActions = useCallback(async (gId: string) => {
    try {
      const actions = await api.getAvailableActions(gId);
      setAvailableActions(actions);
    } catch (err) {
      console.error('Failed to load available actions:', err);
    }
  }, []);

  // Handle action response
  const handleActionResponse = useCallback((response: { state: ApiGameState; events: ApiEvent[] }, gId: string) => {
    setGameState(response.state);
    setEvents(prev => [...response.events, ...prev]);
    refreshAvailableActions(gId);
  }, [refreshAvailableActions]);

  // Create a new game
  const createGame = useCallback(async (newGameId: string) => {
    setIsLoading(true);
    setError(null);
    try {
      const response = await api.createGameLegacy(newGameId);
      setGameId(newGameId);
      setGameState(response.state);
      setEvents([]);
      await refreshAvailableActions(newGameId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create game');
    } finally {
      setIsLoading(false);
    }
  }, [refreshAvailableActions]);

  // Load an existing game
  const loadGame = useCallback(async (existingGameId: string) => {
    setIsLoading(true);
    setError(null);
    try {
      const response = await api.getGame(existingGameId);
      setGameId(existingGameId);
      setGameState(response.state);
      await refreshAvailableActions(existingGameId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load game');
    } finally {
      setIsLoading(false);
    }
  }, [refreshAvailableActions]);

  // Purchase units
  const purchase = useCallback(async (purchases: Record<string, number>) => {
    if (!gameId) return;
    setIsLoading(true);
    setError(null);
    try {
      const response = await api.purchase(gameId, purchases);
      handleActionResponse(response, gameId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Purchase failed');
    } finally {
      setIsLoading(false);
    }
  }, [gameId, handleActionResponse]);

  // Move units
  const move = useCallback(async (from: string, to: string, unitIds: string[]) => {
    if (!gameId) return;
    setIsLoading(true);
    setError(null);
    try {
      const response = await api.move(gameId, from, to, unitIds);
      handleActionResponse(response, gameId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Move failed');
    } finally {
      setIsLoading(false);
    }
  }, [gameId, handleActionResponse]);

  // Initiate combat
  const initiateCombat = useCallback(async (territoryId: string) => {
    if (!gameId) return;
    setIsLoading(true);
    setError(null);
    try {
      const response = await api.initiateCombat(gameId, territoryId);
      handleActionResponse(response, gameId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Combat initiation failed');
    } finally {
      setIsLoading(false);
    }
  }, [gameId, handleActionResponse]);

  // Continue combat
  const continueCombat = useCallback(async () => {
    if (!gameId) return;
    setIsLoading(true);
    setError(null);
    try {
      const response = await api.continueCombat(gameId);
      handleActionResponse(response, gameId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Continue combat failed');
    } finally {
      setIsLoading(false);
    }
  }, [gameId, handleActionResponse]);

  // Retreat
  const retreat = useCallback(async (retreatTo: string) => {
    if (!gameId) return;
    setIsLoading(true);
    setError(null);
    try {
      const response = await api.retreat(gameId, retreatTo);
      handleActionResponse(response, gameId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Retreat failed');
    } finally {
      setIsLoading(false);
    }
  }, [gameId, handleActionResponse]);

  // Mobilize
  const mobilize = useCallback(async (destination: string, units: { unit_id: string; count: number }[]) => {
    if (!gameId) return;
    setIsLoading(true);
    setError(null);
    try {
      const response = await api.mobilize(gameId, destination, units);
      handleActionResponse(response, gameId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Mobilization failed');
    } finally {
      setIsLoading(false);
    }
  }, [gameId, handleActionResponse]);

  // End phase
  const endPhase = useCallback(async () => {
    if (!gameId) return;
    setIsLoading(true);
    setError(null);
    try {
      const response = await api.endPhase(gameId);
      handleActionResponse(response, gameId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'End phase failed');
    } finally {
      setIsLoading(false);
    }
  }, [gameId, handleActionResponse]);

  // End turn
  const endTurn = useCallback(async () => {
    if (!gameId) return;
    setIsLoading(true);
    setError(null);
    try {
      const response = await api.endTurn(gameId);
      handleActionResponse(response, gameId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'End turn failed');
    } finally {
      setIsLoading(false);
    }
  }, [gameId, handleActionResponse]);

  // Clear error
  const clearError = useCallback(() => {
    setError(null);
  }, []);

  return {
    gameId,
    gameState,
    definitions,
    availableActions,
    isLoading,
    error,
    events,
    createGame,
    loadGame,
    purchase,
    move,
    initiateCombat,
    continueCombat,
    retreat,
    mobilize,
    endPhase,
    endTurn,
    clearError,
  };
}

export default useGame;
