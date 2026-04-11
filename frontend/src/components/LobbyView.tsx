import { useState, useEffect, useCallback, useMemo } from 'react';
import { Link } from 'react-router-dom';
import api, { type GameMeta, type AuthPlayer, type Definitions } from '../services/api';
import './LobbyView.css';

const ALLIANCE_ORDER = ['good', 'evil'];

const LOBBY_POLL_MS = 3000;

/** Matches backend LOBBY_COMPUTER_PLAYER_ID — lobby_claims value for AI-controlled slot. */
const LOBBY_COMPUTER_ID = '__computer__';

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
  const [claimingAlliance, setClaimingAlliance] = useState<string | null>(null);
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

  const handleLobbyComputer = async (factionId: string, computer: boolean) => {
    setClaimError(null);
    setClaimingFactionId(factionId);
    try {
      await api.lobbyAssignComputer(gameId, factionId, computer);
      await refreshMeta();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Update failed';
      const isRouteOrMissing =
        msg === 'Not Found' ||
        msg === 'Game not found' ||
        (msg.toLowerCase().includes('not found') && msg.length < 40);
      setClaimError(
        isRouteOrMissing
          ? 'Server could not run this action (try restarting the backend, then refresh).'
          : msg,
      );
    } finally {
      setClaimingFactionId(null);
    }
  };

  /** Factions in turn order grouped by alliance (for single-player "set all" row). */
  const alliancesWithFactions = useMemo(() => {
    const order = turnOrder.length ? turnOrder : Object.keys(definitions?.factions ?? {});
    const factions = definitions?.factions ?? {};
    const map: Record<string, string[]> = {};
    for (const fid of order) {
      const alliance = (factions[fid] as { alliance?: string } | undefined)?.alliance ?? 'neutral';
      if (!map[alliance]) map[alliance] = [];
      map[alliance].push(fid);
    }
    const result: { alliance: string; factionIds: string[] }[] = [];
    for (const a of ALLIANCE_ORDER) {
      if (map[a]?.length) result.push({ alliance: a, factionIds: map[a] });
    }
    for (const a of Object.keys(map).sort()) {
      if (!ALLIANCE_ORDER.includes(a)) result.push({ alliance: a, factionIds: map[a] });
    }
    return result;
  }, [definitions?.factions, turnOrder]);

  const handleSetAlliance = async (allianceKey: string, claim: boolean) => {
    const entry = alliancesWithFactions.find((e) => e.alliance === allianceKey);
    if (!entry?.factionIds.length) return;
    setClaimError(null);
    setClaimingAlliance(allianceKey);
    try {
      for (const fid of entry.factionIds) {
        await api.claimFaction(gameId, fid, claim);
      }
      await refreshMeta();
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Claim failed';
      setClaimError(msg === 'Not Found' ? 'Game not found or server error. Try refreshing the page.' : msg);
    } finally {
      setClaimingAlliance(null);
    }
  };

  const handleStartClick = () => setStartConfirmOpen(true);
  const handleStartConfirmCancel = () => setStartConfirmOpen(false);
  const handleStartConfirmOk = async () => {
    if (!canStartGame) {
      setStartConfirmOpen(false);
      return;
    }
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
  const isSinglePlayer = !meta.game_code;
  const allFactionsClaimed = isSinglePlayer
    ? order.length > 0 && order.some((fid) => lobbyClaims[fid])
    : order.length > 0 && order.every((fid) => lobbyClaims[fid]);

  /** Distinct human user IDs with at least one faction (multiplayer only; excludes AI slots). */
  const multiplayerHumanPlayerCount = (() => {
    if (isSinglePlayer) return 0;
    const seen = new Set<string>();
    for (const fid of order) {
      const c = lobbyClaims[fid];
      if (c && c !== LOBBY_COMPUTER_ID) seen.add(c);
    }
    return seen.size;
  })();

  const canStartGame =
    isSinglePlayer
      ? allFactionsClaimed
      : allFactionsClaimed && multiplayerHumanPlayerCount >= 2;

  const myClaimedAlliance = (() => {
    if (isSinglePlayer) return null;
    for (const fid of order) {
      if (lobbyClaims[fid] === String(player?.id)) {
        const f = factions[fid] as { alliance?: string } | undefined;
        return f?.alliance ?? null;
      }
    }
    return null;
  })();

  const playerDisplayName = (player != null && meta.player_usernames?.[String(player.id)]) ?? 'You';

  return (
    <div className="lobby-view-overlay">
      <div className="lobby-view">
        <header className="lobby-view__header">
          <h1 className="lobby-view__title">
            {isSinglePlayer ? 'Single player' : 'Lobby'}: {meta.name}
          </h1>
          {meta.game_code && (
            <p className="lobby-view__code">
              JOIN CODE: <strong className="lobby-view__code-value">{meta.game_code}</strong>
            </p>
          )}
        </header>

        <section className="lobby-view__factions" aria-label="Faction assignments">
          {claimError && (
            <p className="lobby-view__error" role="alert">
              {claimError}
            </p>
          )}
          {isSinglePlayer && alliancesWithFactions.length > 0 && (
            <div className="lobby-view__alliance-shortcuts">
              {alliancesWithFactions.map(({ alliance, factionIds }) => {
                const allMe = factionIds.every((fid) => lobbyClaims[fid] === String(player?.id));
                const allComputer = factionIds.every((fid) => !lobbyClaims[fid]);
                const loading = claimingAlliance === alliance;
                const label = alliance === 'good' ? 'Good' : alliance === 'evil' ? 'Evil' : alliance;
                return (
                  <div key={alliance} className="lobby-view__alliance-row" role="group" aria-label={`Set all ${label} factions`}>
                    <span className="lobby-view__alliance-label">Set all {label}</span>
                    <div className="lobby-view__binary-pills">
                      <button
                        type="button"
                        className={`lobby-view__pill lobby-view__pill--you ${allMe ? 'lobby-view__pill--selected' : ''}`}
                        onClick={() => handleSetAlliance(alliance, true)}
                        disabled={loading}
                        aria-pressed={allMe}
                      >
                        {loading ? '…' : playerDisplayName}
                      </button>
                      <button
                        type="button"
                        className={`lobby-view__pill lobby-view__pill--computer ${allComputer ? 'lobby-view__pill--selected' : ''}`}
                        onClick={() => handleSetAlliance(alliance, false)}
                        disabled={loading}
                        aria-pressed={allComputer}
                      >
                        {loading ? '…' : 'Computer'}
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
          <ul className="lobby-view__faction-list">
            {order.map((fid) => {
              const f = factions[fid] as { display_name?: string; icon?: string; alliance?: string; color?: string } | undefined;
              const displayName = f?.display_name ?? fid;
              const icon = f?.icon ?? `${fid}.png`;
              const factionColor = f?.color ?? undefined;
              const claimantId = lobbyClaims[fid];
              const claimantName = claimantId ? (playerUsernames[claimantId] ?? 'Player') : null;
              const isClaimedByMe = claimantId === String(player?.id);
              const isClaimedByComputer = claimantId === LOBBY_COMPUTER_ID;
              const blockedByOtherHuman =
                claimantId != null &&
                claimantId !== String(player?.id) &&
                claimantId !== LOBBY_COMPUTER_ID;
              const alliance = f?.alliance ?? 'neutral';
              const canClaimHuman =
                !blockedByOtherHuman &&
                (isSinglePlayer || myClaimedAlliance == null || myClaimedAlliance === alliance);
              const loading = claimingFactionId === fid;

              return (
                <li key={fid} className="lobby-view__faction-row">
                  <div className="lobby-view__faction-logo-wrap">
                    {factionColor && (
                      <div
                        className="lobby-view__faction-color-bar"
                        style={{ backgroundColor: factionColor }}
                        aria-hidden
                      />
                    )}
                    <div className="lobby-view__faction-logo">
                      <img src={`/assets/factions/${icon}`} alt="" />
                    </div>
                  </div>
                  <div className="lobby-view__faction-info">
                    <span className="lobby-view__faction-name">{displayName}</span>
                    {!isSinglePlayer && claimantId && (
                      <span className="lobby-view__faction-claimant">
                        {isClaimedByMe ? 'You' : isClaimedByComputer ? 'Computer' : claimantName}
                      </span>
                    )}
                  </div>
                  <div className="lobby-view__faction-actions">
                    {isSinglePlayer ? (
                      <div className="lobby-view__binary-pills" role="group" aria-label={`${displayName}: you or computer`}>
                        <button
                          type="button"
                          className={`lobby-view__pill lobby-view__pill--you ${isClaimedByMe ? 'lobby-view__pill--selected' : ''}`}
                          onClick={() => handleClaim(fid, true)}
                          disabled={loading}
                          aria-pressed={isClaimedByMe}
                        >
                          {loading && isClaimedByMe ? '…' : playerDisplayName}
                        </button>
                        <button
                          type="button"
                          className={`lobby-view__pill lobby-view__pill--computer ${!isClaimedByMe ? 'lobby-view__pill--selected' : ''}`}
                          onClick={() => handleClaim(fid, false)}
                          disabled={loading}
                          aria-pressed={!isClaimedByMe}
                        >
                          {loading && !isClaimedByMe ? '…' : 'Computer'}
                        </button>
                      </div>
                    ) : (
                      <div className="lobby-view__multiplayer-actions">
                        {isHost && (!claimantId || isClaimedByComputer) && (
                          <button
                            type="button"
                            className={`lobby-view__pill lobby-view__pill--computer lobby-view__pill--compact${isClaimedByComputer ? ' lobby-view__pill--computer-selected' : ''}`}
                            onClick={() => handleLobbyComputer(fid, !isClaimedByComputer)}
                            disabled={loading}
                            aria-pressed={isClaimedByComputer}
                          >
                            {loading ? '…' : 'Computer'}
                          </button>
                        )}
                        {isClaimedByMe ? (
                          <button
                            type="button"
                            className="lobby-view__pill lobby-view__pill--claimed lobby-view__pill--compact"
                            onClick={() => handleClaim(fid, false)}
                            disabled={loading}
                          >
                            {loading ? '…' : 'Unclaim'}
                          </button>
                        ) : blockedByOtherHuman ? (
                          <span className="lobby-view__pill lobby-view__pill--taken lobby-view__pill--compact">Claimed</span>
                        ) : canClaimHuman ? (
                          <button
                            type="button"
                            className="lobby-view__pill lobby-view__pill--claim lobby-view__pill--compact"
                            onClick={() => handleClaim(fid, true)}
                            disabled={loading}
                          >
                            {loading ? '…' : 'Claim'}
                          </button>
                        ) : null}
                      </div>
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
                disabled={!canStartGame}
                title={
                  !canStartGame
                    ? isSinglePlayer
                      ? 'Assign yourself to at least one faction to start'
                      : !allFactionsClaimed
                        ? 'Claim all factions to start'
                        : 'At least two players must claim factions before you can start'
                    : undefined
                }
              >
                Start game
              </button>
                {startConfirmOpen && (
                  <div className="lobby-view__confirm-overlay" role="dialog" aria-modal="true">
                    <div className="lobby-view__confirm-box">
                      <p className="lobby-view__confirm-text">
                        {isSinglePlayer
                          ? 'Start the game? Faction assignments cannot be changed after this.'
                          : 'Are you sure you want to start the game? Players cannot join and faction assignments cannot be modified once the game has started.'}
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
