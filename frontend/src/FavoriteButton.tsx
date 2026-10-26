import { t } from './i18n';
import Icon from './Icon';

export default function FavoriteButton({ selected, disabled, onClick }: {
  selected: boolean; disabled?: boolean; onClick: () => void;
}) {
  const label = t(selected ? 'Убрать из избранного' : 'Добавить в избранное');
  return <button type="button" className={`icon-button favorite-button ${selected ? 'selected' : ''}`}
    aria-label={label} title={label} aria-pressed={selected} disabled={disabled} onClick={onClick}>
    <Icon name="star" />
  </button>;
}
