import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { Link } from 'react-router-dom';
import api, { type GameMeta, type AuthPlayer, type Definitions, type AiFactionSetting, type AiLevelChoice, type FriendUser } from '../services/api';
import { useMenuScroll } from './MenuScrollContext';
import './LobbyView.css';

const ALLIANCE_ORDER = ['good', 'evil'];

const LOBBY_POLL_MS = 3000;

/** Matches backend LOBBY_COMPUTER_PLAYER_ID — lobby_claims value for AI-controlled slot. */
const LOBBY_COMPUTER_ID = '__computer__';

const WEIGHT_FIELDS: { key: keyof AiFactionSetting['weights']; label: string; max?: number }[] = [
  { key: 'units', label: 'Units' },
  { key: 'stronghold', label: 'Stronghold' },
  { key: 'capital', label: 'Capital' },
  { key: 'power_prod', label: 'Power' },
  { key: 'board_scale', label: 'Scale' },
  { key: 'purchase_attack', label: 'Purchase attack', max: 1 },
];

const FALLBACK_LEVELS: AiLevelChoice[] = [
  { id: 'easy', name: 'Easy' },
  { id: 'normal', name: 'Normal' },
  { id: 'hard', name: 'Hard' },
];

function DifficultyPills({
  levels,
  selected,
  disabled,
  label,
  onSelect,
}: {
  levels: AiLevelChoice[] | undefined;
  selected: string | null | undefined;
  disabled: boolean;
  label: string;
  onSelect: (id: string) => void;
}) {
  const choices = levels && levels.length > 0 ? levels : FALLBACK_LEVELS;
  return (
    <div className="lobby-view__difficulty-pills" role="group" aria-label={label}>
      {choices.map((level) => (
        <button
          key={level.id}
          type="button"
          className={`lobby-view__pill lobby-view__pill--difficulty${selected === level.id ? ' lobby-view__pill--selected' : ''}`}
          onClick={() => onSelect(level.id)}
          disabled={disabled}
          aria-pressed={selected === level.id}
        >
          {level.name}
        </button>
      ))}
    </div>
  );
}

function FactionSearchSettings({
  setting,
  levels,
  disabled,
  onDifficulty,
  onAlgorithm,
  onWeight,
}: {
  setting: AiFactionSetting | undefined;
  levels: AiLevelChoice[];
  disabled: boolean;
  onDifficulty: (difficulty: string) => void;
  onAlgorithm: (algorithm: string) => void;
  onWeight: (key: keyof AiFactionSetting['weights'], value: number) => void;
}) {
  if (!setting) return <span className="lobby-view__waiting">Loading…</span>;
  const choices = levels.length > 0 ? levels : FALLBACK_LEVELS;
  const known = choices.some((level) => level.id === setting.difficulty);
  const usesMcts = (setting.algorithm || 'mcts') === 'mcts';
  return (
    <div className="lobby-ai">
      <label className="lobby-ai__field">
        Difficulty
        <select
          value={setting.difficulty}
          disabled={disabled}
          onChange={(e) => onDifficulty(e.target.value)}
        >
          {!known && <option value={setting.difficulty}>{setting.difficulty}</option>}
          {choices.map((level) => (
            <option key={level.id} value={level.id}>
              {level.name}
            </option>
          ))}
        </select>
      </label>
      <label className="lobby-ai__field">
        Algorithm
        <select
          value={setting.algorithm || 'mcts'}
          disabled={disabled}
          onChange={(e) => onAlgorithm(e.target.value)}
        >
          <option value="mcts">MCTS</option>
          <option value="heuristic">Heuristic</option>
        </select>
      </label>
      {usesMcts ? WEIGHT_FIELDS.map((field) => (
        <label key={field.key} className="lobby-ai__field">
          {field.label}
          <input
            type="number"
            min={0}
            max={field.max}
            step={0.1}
            value={setting.weights[field.key]}
            disabled={disabled}
            onChange={(e) => {
              const value = Number(e.target.value);
              if (Number.isFinite(value)) onWeight(field.key, value);
            }}
          />
        </label>
      )) : null}
    </div>
  );
}

function allianceDisplayLabel(alliance: string): string {
  const a = alliance.trim();
  if (!a) return 'Neutral';
  if (a === 'good') return 'Good';
  if (a === 'evil') return 'Evil';
  if (a === 'neutral') return 'Neutral';
  return a
    .split(/[_\s]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase())
    .join(' ');
}

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
  const { navigateWithScroll } = useMenuScroll();
  const [player, setPlayer] = useState<AuthPlayer | null>(null);
  const [claimingFactionId, setClaimingFactionId] = useState<string | null>(null);
  const [claimingAlliance, setClaimingAlliance] = useState<string | null>(null);
  const [claimError, setClaimError] = useState<string | null>(null);
  const [startConfirmOpen, setStartConfirmOpen] = useState(false);
  const [starting, setStarting] = useState(false);
  const [factionSettings, setFactionSettings] = useState<Record<string, AiFactionSetting> | null>(null);
  const [savingDifficulty, setSavingDifficulty] = useState(false);
  const saveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const saveGen = useRef(0);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteFriends, setInviteFriends] = useState<FriendUser[] | null>(null);
  const [invitedIds, setInvitedIds] = useState<string[]>([]);
  const [inviteSelected, setInviteSelected] = useState<string[]>([]);
  const [inviteLoading, setInviteLoading] = useState(false);
  const [inviteSending, setInviteSending] = useState(false);
  const [inviteError, setInviteError] = useState<string | null>(null);

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

  useEffect(() => {
    if (meta.computer_only && meta.ai_faction_settings && factionSettings == null) {
      setFactionSettings(meta.ai_faction_settings);
    }
  }, [meta.computer_only, meta.ai_faction_settings, factionSettings]);

  const scheduleSettingsSave = (next: Record<string, AiFactionSetting>) => {
    if (saveTimer.current) clearTimeout(saveTimer.current);
    const gen = ++saveGen.current;
    saveTimer.current = setTimeout(() => {
      api.updateAiFactionSettings(gameId, next)
        .then((res) => {
          if (saveGen.current !== gen) return;
          setFactionSettings(res.ai_faction_settings);
          onMetaUpdate({ ...meta, ai_faction_settings: res.ai_faction_settings });
        })
        .catch((err) => {
          if (saveGen.current !== gen) return;
          setClaimError(err instanceof Error ? err.message : 'Could not save computer settings');
        });
    }, 400);
  };

  const updateFactionSetting = (
    factionId: string,
    patch: {
      difficulty?: string;
      algorithm?: string;
      weight?: { key: keyof AiFactionSetting['weights']; value: number };
    },
  ) => {
    setFactionSettings((current) => {
      if (!current?.[factionId]) return current;
      const row = current[factionId];
      const nextRow: AiFactionSetting = {
        ...row,
        ...(patch.difficulty != null ? { difficulty: patch.difficulty } : {}),
        ...(patch.algorithm != null ? { algorithm: patch.algorithm } : {}),
        ...(patch.weight ? { weights: { ...row.weights, [patch.weight.key]: patch.weight.value } } : {}),
      };
      const next = { ...current, [factionId]: nextRow };
      scheduleSettingsSave(next);
      return next;
    });
  };

  const handleFactionDifficulty = async (factionId: string, difficulty: string) => {
    const current = meta.ai_faction_difficulties?.[factionId] ?? meta.ai_difficulty;
    if (!difficulty || difficulty === current || savingDifficulty) return;
    setClaimError(null);
    setSavingDifficulty(true);
    try {
      const saved = await api.updateAiDifficulty(gameId, difficulty, factionId);
      onMetaUpdate({
        ...meta,
        ai_difficulty: saved.ai_difficulty,
        ai_faction_difficulties: saved.ai_faction_difficulties ?? meta.ai_faction_difficulties,
      });
    } catch (err) {
      setClaimError(err instanceof Error ? err.message : 'Could not save difficulty');
    } finally {
      setSavingDifficulty(false);
    }
  };

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
  const isComputerOnly = meta.computer_only === true;
  const isSinglePlayer = !meta.game_code && !isComputerOnly;
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

  const canStartGame = isComputerOnly
    ? order.length > 0
    : isSinglePlayer
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

  const memberIds = useMemo(
    () => new Set((meta.players ?? []).map((p) => String(p.player_id))),
    [meta.players],
  );

  const openInvites = () => {
    setInviteOpen((open) => !open);
    setInviteError(null);
    setInviteSelected([]);
    if (inviteFriends != null) return;
    setInviteLoading(true);
    Promise.all([api.listFriends(), api.listLobbyInvites(gameId)])
      .then(([friendsRes, sentRes]) => {
        setInviteFriends(friendsRes.friends ?? []);
        setInvitedIds(sentRes.invitee_ids ?? []);
      })
      .catch((err) => setInviteError(err instanceof Error ? err.message : 'Could not load friends'))
      .finally(() => setInviteLoading(false));
  };

  const toggleInvite = (id: string) => {
    setInviteSelected((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));
  };

  const sendInvites = async () => {
    if (inviteSelected.length === 0) return;
    setInviteSending(true);
    setInviteError(null);
    try {
      await api.sendGameInvites(gameId, inviteSelected);
      setInvitedIds((prev) => [...new Set([...prev, ...inviteSelected])]);
      setInviteSelected([]);
    } catch (err) {
      setInviteError(err instanceof Error ? err.message : 'Could not send invites');
    } finally {
      setInviteSending(false);
    }
  };

  return (
    <div className="lobby-view-overlay">
      <div className={`lobby-view${isSinglePlayer ? ' lobby-view--single-player' : ''}${isComputerOnly ? ' lobby-view--computer' : ''}`}>
        <header className="lobby-view__header">
          <h1 className="lobby-view__title">
            {isComputerOnly ? 'Computer only' : isSinglePlayer ? 'Single player' : 'Lobby'}: {meta.name}
          </h1>
          {isComputerOnly && (
            <p className="lobby-view__code">
              Every faction is the computer. Difficulty is how long MCTS searches. Weights change how it scores armies and land, and they apply when that faction's algorithm is MCTS.
            </p>
          )}
          {meta.game_code && (
            <div className="lobby-view__code-row">
              <button
                type="button"
                className="lobby-view__invite-btn"
                aria-expanded={inviteOpen}
                onClick={openInvites}
              >
                Invite Friends
              </button>
              <p className="lobby-view__code">
                JOIN CODE: <strong className="lobby-view__code-value">{meta.game_code}</strong>
              </p>
            </div>
          )}
          {meta.game_code && inviteOpen && (
            <div className="lobby-view__invite">
              {inviteLoading && <p className="lobby-view__invite-note">Loading friends…</p>}
              {inviteError && (
                <p className="lobby-view__error" role="alert">
                  {inviteError}
                </p>
              )}
              {inviteFriends && inviteFriends.length === 0 && (
                <p className="lobby-view__invite-note">You have no friends to invite yet.</p>
              )}
              {inviteFriends && inviteFriends.length > 0 && (
                <div className="lobby-view__invite-body">
                  <ul className="lobby-view__invite-list" aria-label="Friends to invite">
                    {inviteFriends.map((friend) => {
                      const inLobby = memberIds.has(friend.id);
                      const already = invitedIds.includes(friend.id);
                      const selected = inviteSelected.includes(friend.id);
                      const locked = inLobby || already;
                      return (
                        <li key={friend.id}>
                          <label className={`lobby-view__invite-friend${locked ? ' lobby-view__invite-friend--locked' : ''}`}>
                            <input
                              type="checkbox"
                              className="lobby-view__invite-check"
                              checked={selected || already || inLobby}
                              disabled={locked || inviteSending}
                              onChange={() => toggleInvite(friend.id)}
                            />
                            <span className="lobby-view__invite-name">{friend.username}</span>
                            {inLobby ? <span className="lobby-view__invite-tag">In lobby</span> : null}
                            {!inLobby && already ? <span className="lobby-view__invite-tag">Invited</span> : null}
                          </label>
                        </li>
                      );
                    })}
                  </ul>
                  {inviteSelected.length > 0 && (
                    <button
                      type="button"
                      className="lobby-view__invite-send primary"
                      disabled={inviteSending}
                      onClick={() => void sendInvites()}
                    >
                      {inviteSending ? 'Sending…' : 'Send'}
                    </button>
                  )}
                </div>
              )}
            </div>
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
              const allianceLabel = allianceDisplayLabel(alliance);
              const canClaimHuman =
                !blockedByOtherHuman &&
                (isSinglePlayer || myClaimedAlliance == null || myClaimedAlliance === alliance);
              const loading = claimingFactionId === fid;
              const factionRowLabel = `${displayName} | ${allianceLabel}`;

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
                    <div
                      className="lobby-view__faction-logo faction-seal"
                      style={factionColor ? { borderColor: factionColor } : undefined}
                    >
                      <img src={`/assets/factions/${icon}`} alt="" />
                    </div>
                  </div>
                  <div className="lobby-view__faction-info">
                    <div className="lobby-view__faction-title" title={factionRowLabel}>
                      <span className="lobby-view__faction-name">{displayName}</span>
                      <span className="lobby-view__faction-title-sep" aria-hidden>
                        |
                      </span>
                      <span className="lobby-view__faction-alliance">{allianceLabel}</span>
                    </div>
                    {!isSinglePlayer && claimantId && (
                      <span className="lobby-view__faction-claimant">
                        {isClaimedByMe ? 'You' : isClaimedByComputer ? 'Computer' : claimantName}
                      </span>
                    )}
                  </div>
                  <div className="lobby-view__faction-actions">
                    {isComputerOnly ? (
                      <FactionSearchSettings
                        setting={factionSettings?.[fid]}
                        levels={meta.ai_levels ?? []}
                        disabled={!isHost || claimingFactionId === fid}
                        onDifficulty={(difficulty) => updateFactionSetting(fid, { difficulty })}
                        onAlgorithm={(algorithm) => updateFactionSetting(fid, { algorithm })}
                        onWeight={(key, value) => updateFactionSetting(fid, { weight: { key, value } })}
                      />
                    ) : isSinglePlayer ? (
                      <div className="lobby-view__single-choice">
                        <div
                          className="lobby-view__binary-pills"
                          role="group"
                          aria-label={`${factionRowLabel}: you or computer`}
                        >
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
                        {!isClaimedByMe && (
                          <DifficultyPills
                            levels={meta.ai_levels}
                            selected={meta.ai_faction_difficulties?.[fid] ?? meta.ai_difficulty}
                            disabled={!isHost || savingDifficulty}
                            label={`${displayName} difficulty`}
                            onSelect={(id) => void handleFactionDifficulty(fid, id)}
                          />
                        )}
                      </div>
                    ) : (
                      <div className="lobby-view__single-choice">
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
                        {isClaimedByComputer && (
                          <DifficultyPills
                            levels={meta.ai_levels}
                            selected={meta.ai_faction_difficulties?.[fid] ?? meta.ai_difficulty}
                            disabled={!isHost || savingDifficulty}
                            label={`${displayName} difficulty`}
                            onSelect={(id) => void handleFactionDifficulty(fid, id)}
                          />
                        )}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </section>

        <footer className="lobby-view__footer">
          <Link
            to="/"
            className="lobby-view__menu-btn"
            onClick={(e) => {
              e.preventDefault();
              navigateWithScroll('/');
            }}
          >
            Menu
          </Link>
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
                    ? isComputerOnly
                      ? 'This scenario has no factions'
                      : isSinglePlayer
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
