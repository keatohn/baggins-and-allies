import { useState, useEffect, useCallback } from 'react';
import { Link } from 'react-router-dom';
import api, { type GameMeta, type AuthPlayer, type Definitions } from '../services/api';
import './LobbyView.css';

const LOBBY_POLL_MS = 3000;

interface LobbyViewProps {
  gameId: string;
  meta: GameMeta;
  definitions: Definitions | null;
  turnOrder: string[];
  onMetaUpdate: (meta: GameMeta) => void;
  onGameStarted: () => void;
}

export default function LobbyView({
  gameId,
  meta,
  definitions,
  turnOrder,
  onMetaUpdate,
  onGameStarted,
}: LobbyViewProps) {
  const [player, setPlayer] = useState<AuthPlayer | null>(null);
  const [claimingFactionId, setClaimingFactionId] = useState<string | null>(null);
  const [claimError, setClaimError] = useState<string | null>(null);
  const [startConfirmOpen, setStartConfirmOpen] = useState(false);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    api.authMe().then(setPlayer).catch(() => setPlayer(null));
  }, []);

  const refreshMeta = useCallback(() => {
    api.getGameMeta(gameId).then((m) => {
      onMetaUpdate(m);
      if (m.status !== 'lobby') onGameStarted();
    }).catch(() => { });
  }, [gameId, onMetaUpdate, onGameStarted]);

  useEffect(() => {
    const t = setInterval(refreshMeta, LOBBY_POLL_MS);
    return () => clearInterval(t);
  }, [refreshMeta]);

  const lobbyClaims = meta.lobby_claims ?? {};
  const playerUsernames = meta.player_usernames ?? {};
  const isHost = player != null && meta.created_by != null && String(meta.created_by) === String(player.id);

  const handleClaim = async (factionId: string, claim: boolean) => {
    setClaimError(null);
    setClaimingFactionId(factionId);
    try {
      await api.claimFaction(gameId, factionId, claim);
      await refreshMeta();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Claim failed';
      setClaimError(msg === 'Not Found' ? 'Game not found or server error. Try refreshing the page.' : msg);
    } finally {
      setClaimingFactionId(null);
    }
  };

  const handleStartClick = () => setStartConfirmOpen(true);
  const handleStartConfirmCancel = () => setStartConfirmOpen(false);
  const handleStartConfirmOk = async () => {
    setStarting(true);
    setClaimError(null);
    try {
      await api.startGame(gameId);
      await refreshMeta();
      setStartConfirmOpen(false);
      onGameStarted();
    } catch (err) {
      setClaimError(err instanceof Error ? err.message : 'Start failed');
    } finally {
      setStarting(false);
    }
  };

  const factions = definitions?.factions ?? {};
  const order = turnOrder.length ? turnOrder : Object.keys(factions);
  const allFactionsClaimed = order.length > 0 && order.every((fid) => lobbyClaims[fid]);
  const myClaimedAlliance = (() => {
    for (const fid of order) {
      if (lobbyClaims[fid] === String(player?.id)) {
        const f = factions[fid] as { alliance?: string } | undefined;
        return f?.alliance ?? null;
      }
    }
    return null;
  })();

  const scenario = meta.scenario;
  const context = scenario?.context as { year?: string; map?: string; factions?: string[] } | undefined;

  return (
    <div className="lobby-view-overlay">
      <div className="lobby-view">
        <header className="lobby-view__header">
          <h1 className="lobby-view__title">Lobby: {meta.name}</h1>
          {meta.game_code && (
            <p className="lobby-view__code">
              JOIN CODE: <strong className="lobby-view__code-value">{meta.game_code}</strong>
            </p>
          )}
        </header>

        {scenario && (
          <section className="lobby-view__scenario">
            <h2 className="lobby-view__section-title">Scenario</h2>
            <p className="lobby-view__scenario-name">{scenario.display_name}</p>
            {(context?.year || context?.map || (context?.factions?.length)) && (
              <p className="lobby-view__scenario-desc">
                {[context?.year, context?.map].filter(Boolean).join(' · ')}
                {Array.isArray(context?.factions) && context.factions.length > 0 && (
                  <span className="lobby-view__scenario-factions">
                    {' · '}{context.factions.join(', ')}
                  </span>
                )}
              </p>
            )}
          </section>
        )}

        <section className="lobby-view__factions">
          <h2 className="lobby-view__section-title">Factions</h2>
          {claimError && (
            <p className="lobby-view__error" role="alert">
              {claimError}
            </p>
          )}
          <ul className="lobby-view__faction-list">
            {order.map((fid) => {
              const f = factions[fid] as { display_name?: string; icon?: string; alliance?: string } | undefined;
              const displayName = f?.display_name ?? fid;
              const icon = f?.icon ?? `${fid}.png`;
              const claimantId = lobbyClaims[fid];
              const claimantName = claimantId ? (playerUsernames[claimantId] ?? 'Player') : null;
              const isClaimedByMe = claimantId === String(player?.id);
              const isClaimedByOther = claimantId != null && !isClaimedByMe;
              const alliance = f?.alliance ?? 'neutral';
              const canClaim =
                !isClaimedByOther &&
                (myClaimedAlliance == null || myClaimedAlliance === alliance);
              const loading = claimingFactionId === fid;

              return (
                <li key={fid} className="lobby-view__faction-row">
                  <div className="lobby-view__faction-logo">
                    <img src={`/assets/factions/${icon}`} alt="" />
                  </div>
                  <div className="lobby-view__faction-info">
                    <span className="lobby-view__faction-name">{displayName}</span>
                    {claimantName && (
                      <span className="lobby-view__faction-claimant">
                        {isClaimedByMe ? 'You' : claimantName}
                      </span>
                    )}
                  </div>
                  <div className="lobby-view__faction-actions">
                    {isClaimedByMe ? (
                      <button
                        type="button"
                        className="lobby-view__pill lobby-view__pill--claimed"
                        onClick={() => handleClaim(fid, false)}
                        disabled={loading}
                      >
                        {loading ? '…' : 'Unclaim'}
                      </button>
                    ) : isClaimedByOther ? (
                      <span className="lobby-view__pill lobby-view__pill--taken">Claimed</span>
                    ) : canClaim ? (
                      <button
                        type="button"
                        className="lobby-view__pill lobby-view__pill--claim"
                        onClick={() => handleClaim(fid, true)}
                        disabled={loading}
                      >
                        {loading ? '…' : 'Claim'}
                      </button>
                    ) : (
                      <span className="lobby-view__pill lobby-view__pill--disabled" title="Claim factions from one alliance only">
                        Enemy alliance
                      </span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>

        <footer className="lobby-view__footer">
          <Link to="/" className="lobby-view__menu-btn">Menu</Link>
          <div className="lobby-view__start-area">
          {player == null ? (
            <span className="lobby-view__waiting">Loading…</span>
          ) : isHost ? (
            <>
              <button
                type="button"
                className="lobby-view__start-btn primary"
                onClick={handleStartClick}
                disabled={!allFactionsClaimed}
                title={!allFactionsClaimed ? 'Claim all factions to start' : undefined}
              >
                Start game
              </button>
                {startConfirmOpen && (
                  <div className="lobby-view__confirm-overlay" role="dialog" aria-modal="true">
                    <div className="lobby-view__confirm-box">
                      <p className="lobby-view__confirm-text">
                        Are you sure you want to start the game? Players cannot join and faction
                        assignments cannot be modified once the game has started.
                      </p>
                      <div className="lobby-view__confirm-actions">
                        <button
                          type="button"
                          className="lobby-view__confirm-btn"
                          onClick={handleStartConfirmCancel}
                          disabled={starting}
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          className="lobby-view__confirm-btn primary"
                          onClick={handleStartConfirmOk}
                          disabled={starting}
                        >
                          {starting ? 'Starting…' : 'Start game'}
                        </button>
                      </div>
                    </div>
                  </div>
                )}
              </>
            ) : (
              <span className="lobby-view__waiting">Waiting for host to start game</span>
            )}
          </div>
        </footer>
      </div>
    </div>
  );
}
