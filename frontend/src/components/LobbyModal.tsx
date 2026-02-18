import type { GameMeta } from '../services/api';
import './LobbyModal.css';

interface LobbyModalProps {
  meta: GameMeta;
  onClose: () => void;
}

export default function LobbyModal({ meta, onClose }: LobbyModalProps) {
  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal lobby-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <h2>Lobby: {meta.name}</h2>
          <button type="button" className="close-btn" onClick={onClose}>×</button>
        </header>
        <div className="lobby-modal-body">
          {meta.game_code && (
            <p className="lobby-modal-code">
              Share this code for others to join: <strong>{meta.game_code}</strong>
            </p>
          )}
          <p className="lobby-modal-placeholder">
            Assign factions and game options will go here.
          </p>
          <p className="lobby-modal-note">
            Close this to view the game. Starting the game will be added later.
          </p>
        </div>
      </div>
    </div>
  );
}
