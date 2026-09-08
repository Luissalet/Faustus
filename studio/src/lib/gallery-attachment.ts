import {t} from '../i18n';

/** Gallery deep links may download images, never arbitrary remote resources. */
export async function galleryAttachment(url:string, name:string, origin:string, signal?:AbortSignal):Promise<File> {
  const target = new URL(url, origin);
  if(target.origin !== origin || !['http:', 'https:'].includes(target.protocol) || target.username || target.password)
    throw new Error(t('Only images from this Faustus instance can be attached from the gallery.'));
  const response = await fetch(target.href, {credentials:'same-origin', redirect:'error', signal});
  if(!response.ok) throw new Error(t('Could not load the gallery image ({status}).', {status:response.status}));
  const blob = await response.blob();
  if(!blob.type.startsWith('image/') || !blob.size)
    throw new Error(t('The gallery did not return an image.'));
  return new File([blob], name, {type:blob.type});
}
