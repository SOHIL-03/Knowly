import { expect, test, type Page } from '@playwright/test';

/**
 * Acceptance journey (prompt section 15 / Architecture v3 section 15.1) in a real browser:
 * sample sign-in -> onboarding -> live progress -> Today -> text player -> checkpoint Ask with
 * citations -> recall quiz changes mastery -> Knowledge -> Library -> persistence -> delete account.
 * Gemini is the deterministic test fake (see backend/tests/e2e_server.py).
 */

async function onboard(page: Page) {
  await page.goto('/');
  await page.getByRole('button', { name: 'Try with a sample newsletter' }).click();
  await expect(page.getByText('Step 1 of 4')).toBeVisible();
  await expect(page.getByText('Cloud Builders Weekly (sample)')).toBeVisible();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'What are you building and learning?' })).toBeVisible();
  await page.getByLabel('Your current project').fill('RAG app on Firestore vector search and Cloud Run with FastAPI');
  await page.getByRole('button', { name: 'vector search' }).click();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'How well do you know these?' })).toBeVisible();
  await page.getByRole('radio', { name: 'Heard of it' }).first().click();
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByRole('heading', { name: 'How should your briefing sound?' })).toBeVisible();
  await page.getByRole('radio', { name: '5 min', exact: true }).click();
  await page.getByLabel('Notify me').selectOption('none');
  await page.getByRole('button', { name: 'Make my first briefing' }).click();
}

test('sample user goes from sign-in to a learning loop and can delete everything', async ({ page }) => {
  await onboard(page);

  // Generating screen is driven by the Firestore listener on the briefing document.
  await expect(page.getByRole('heading', { name: 'Making your briefing' })).toBeVisible();
  await expect(page.getByText(/Reading 3 newsletters/)).toBeVisible();
  await page.waitForURL('**/today');

  // Today: segment cards with reasons, and Play all.
  await expect(page.getByText('For your project')).toBeVisible();
  await expect(page.getByText(/Includes 1 wildcard/)).toBeVisible();
  await expect(page.getByText(/Audio ready|Audio on its way|Making audio/).first()).toBeVisible();
  await page.getByRole('button', { name: 'Play all' }).click();

  // Player: two-host script from Gemini multi-speaker TTS; the checkpoint opens when the audio ends.
  await expect(page.getByText('Segment 1 of')).toBeVisible();
  await expect(page.getByText('Maya', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('Theo', { exact: true }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: /(sample)/ }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Play', exact: true }).click();

  // Checkpoint sheet with countdown; typing pauses it.
  await expect(page.getByRole('heading', { name: 'Ask, go deeper or quiz?' })).toBeVisible();
  await expect(page.getByText(/Continuing in \d seconds/)).toBeVisible();
  await page.getByPlaceholder('Type or speak a question…').fill('What does this mean for my index?');
  await expect(page.getByText('Paused')).toBeVisible();
  await page.getByRole('button', { name: 'Send' }).click();

  // Grounded answer with citation chips resolved to newsletter + link.
  await expect(page.getByText('You asked')).toBeVisible();
  await expect(page.getByRole('button', { name: /Listen to the answer|Pause spoken answer/ })).toBeVisible();
  await expect(page.getByRole('dialog').getByRole('link', { name: /(sample)/ }).first()).toBeVisible();
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByText(/Saved ".*" to your Library/)).toBeVisible();
  await page.getByRole('button', { name: 'Back to briefing' }).click();

  // Recall quiz: no multiple choice; answer, grade, mastery shown.
  await page.getByRole('button', { name: 'Quiz me' }).click();
  await expect(page.getByText(/Quick check/)).toBeVisible();
  await expect(page.getByRole('button', { name: /Hear the question|Pause spoken answer/ })).toBeVisible();
  await page.getByRole('button', { name: 'Hint' }).click();
  await expect(page.getByText(/^Hint:/)).toBeVisible();
  await page.getByRole('button', { name: "I don't know" }).click();
  await expect(page.getByText('Not yet')).toBeVisible();
  await expect(page.getByText(/What Knowly thinks you know about/)).toBeVisible();
  await page.getByRole('button', { name: 'Continue' }).click();

  // Knowledge page reflects the learner model.
  await page.goto('/knowledge');
  await expect(page.getByRole('heading', { name: 'What Knowly thinks you know' })).toBeVisible();
  await expect(page.getByRole('meter').first()).toBeVisible();
  await expect(page.getByText(/signals? ·/).first()).toBeVisible();

  // Library shows the saved story and the Q&A.
  await page.goto('/library');
  await expect(page.getByText(/Saved \d/).first()).toBeVisible();
  await page.getByRole('radio', { name: /Q&A/ }).click();
  await expect(page.getByText('What does this mean for my index?')).toBeVisible();

  // Persistence: a reload keeps the same briefing (data is in Firestore, not localStorage).
  await page.goto('/today');
  await expect(page.getByText('For your project')).toBeVisible();
  await page.evaluate(() => localStorage.clear());
  await page.reload();
  await expect(page.getByText('For your project')).toBeVisible();

  // Delete everything.
  await page.goto('/settings');
  await page.getByLabel('Type DELETE to confirm').fill('DELETE');
  await page.getByRole('button', { name: 'Delete everything' }).click();
  await page.waitForURL((u) => u.pathname === '/');
  await expect(page.getByRole('button', { name: 'Continue with Google' })).toBeVisible();
});
