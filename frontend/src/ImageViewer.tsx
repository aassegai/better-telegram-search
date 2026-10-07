import { useState } from 'react';
import Dialog from './Dialog';
import { t } from './i18n';

export type OpenImage = { id: number; messageId: number };

export default function ImageViewer({ image, onClose }: { image: OpenImage; onClose: () => void }) {
  const [failed, setFailed] = useState(false);
  return <Dialog className="image-dialog" label={t('Просмотр изображения')} closeLabel={t('Закрыть изображение')}
    onClose={onClose} dismissOnBackdrop>
    {failed ? <p role="alert">{t('Изображение недоступно в папке источника')}</p> :
      <img src={`/api/media/${image.id}`} alt={t('Фотография из сообщения')} onError={() => setFailed(true)} />}
    <p className="image-caption">{t('Сообщение #{p0}', { p0: image.messageId })}</p>
  </Dialog>;
}
