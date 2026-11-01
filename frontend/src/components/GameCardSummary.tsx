import type { GameListItem } from '../services/api';
import StrongholdAllianceBar from './StrongholdAllianceBar';
import '../pages/GameList.css';
import './GameCardSummary.css';

function formatPhase(phase: string): string {
  if (phase === 'non_combat_move') return 'Non-Combat Move';
  if (phase === 'mobilization') return 'Mobilization';
  return phase.split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
}

function formatCreatedAt(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  } catch {
    return iso;
  }
}

function truncateUsername(username: string, maxChars: number = 12): string {
  if (username.length <= maxChars) return username;
  return `${username.slice(0, maxChars)}...`;
}

function playerNames(g: GameListItem): string | null {
  const names: string[] = [];
  for (const p of g.players ?? []) {
    const n = p.username?.trim();
    if (!n) continue;
    const label = p.forfeited ? `${n} (forfeited)` : n;
    if (!names.includes(label)) names.push(label);
  }
  if (names.length > 0) return names.join(', ');
  if (g.current_player_username) return g.current_player_username;
  return null;
}

/** Load-game card body: name, turn or lobby, players, stronghold bar. No forfeit or delete. */
export default function GameCardSummary({ game }: { game: GameListItem }) {
  const names = playerNames(game);
  return (
    <div className="game-card-summary">
      <div className="game-list__card-main">
        <h3 className="game-list__name">{game.name}</h3>
        {game.status === 'lobby' ? (
          <div className="game-list__turn-info">
            <span className="game-list__turn-meta game-list__turn-meta--lobby">
              Lobby
              {game.scenario?.display_name && (
                <>
                  <span className="game-list__meta-sep">|</span>
                  <span>{game.scenario.display_name}</span>
                </>
              )}
            </span>
            <span className="game-list__lobby-stats">
              {game.lobby_players ?? 0} {(game.lobby_players ?? 0) === 1 ? 'Player' : 'Players'} | {game.lobby_factions_claimed ?? 0}/{game.lobby_factions_total ?? 0} Factions
            </span>
            {game.created_at && (
              <span className="game-list__created">Created {formatCreatedAt(game.created_at)}</span>
            )}
            {names && (
              <span className="game-list__players" title={names}>
                Players: {names}
              </span>
            )}
          </div>
        ) : (
          <div className="game-list__turn-info">
            {(game.current_faction_icon != null || game.current_faction_display_name != null || (game.current_player_username != null && game.current_player_username !== '')) && (
              <span className="game-list__faction-row">
                {game.current_faction_icon && (
                  <img
                    src={game.current_faction_icon}
                    alt=""
                    className="game-list__faction-icon"
                  />
                )}
                <span className="game-list__faction-name">{game.current_faction_display_name ?? game.current_faction ?? '—'}</span>
                {game.current_player_username != null && game.current_player_username !== '' && (
                  <span className="game-list__player-name" title={game.current_player_username}>
                    {' | '}
                    {truncateUsername(game.current_player_username)}
                  </span>
                )}
              </span>
            )}
            <span className="game-list__turn-meta">
              {game.scenario?.display_name && <span>{game.scenario.display_name}</span>}
              {game.scenario?.display_name && (game.turn_number != null || game.phase) && (
                <span className="game-list__meta-sep">|</span>
              )}
              {game.turn_number != null && <span>Turn {game.turn_number}</span>}
              {game.turn_number != null && game.phase && <span className="game-list__meta-sep">|</span>}
              {game.phase && <span>{formatPhase(game.phase)}</span>}
            </span>
            {game.created_at && (
              <span className="game-list__created">Created {formatCreatedAt(game.created_at)}</span>
            )}
            {names && (
              <span className="game-list__players" title={names}>
                Players: {names}
              </span>
            )}
          </div>
        )}
      </div>
      {game.status !== 'lobby' && game.faction_stats && (
        <div className="game-list__stronghold-bar-wrap">
          <StrongholdAllianceBar factionStats={game.faction_stats} variant="gameList" />
        </div>
      )}
    </div>
  );
}
