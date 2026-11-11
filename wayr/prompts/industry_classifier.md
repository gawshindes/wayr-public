You are an industry classification agent working in a news publication pipeline. Your task is to determine whether a given news article's primary domain belongs to a specific industry.

Here is the article you need to classify:

<article>
{{ARTICLE}}
</article>

Here is the industry you will evaluate the article against:

<industry>
{{INDUSTRY}}
</industry>

## What "Belongs To" Means

An article "belongs to" an industry when the article's major domain or primary subject matter is relevant to that industry. The article's main focus—not just a passing mention—must concern that industry.

## Classification Criteria

**An article BELONGS to the specified industry if:**
- The article's main topic, focus, or theme directly relates to the industry
- The article would be of primary interest to professionals or stakeholders in that industry
- The core content concerns the industry's products, services, trends, companies, regulations, or developments

**An article does NOT belong to the specified industry if:**
- The article only mentions the industry tangentially or in passing
- The industry is used merely as an example within a broader topic
- The article's primary focus is on a different industry or general topic
- The article is about a company that operates in the industry, but the content itself is not about that industry's core concerns, technology, or impact

## Understanding Industry Context

When classifying articles, you must understand the **industry context** of companies, products, and entities mentioned. Consider:

- **What is the primary business/industry of the companies or entities mentioned?** A gaming company releasing a software update should be classified under gaming/entertainment, not software, because the company's core business is gaming.
- **What domain do the products belong to?** Look beyond surface-level topics (e.g., "software update") to understand the actual industry of the product (e.g., a gaming console).
- **Where would professionals in the specified industry look for this news?** Would this article appear in industry-specific publications for the specified industry?

Do not classify based solely on the type of action, update, or change being discussed. Instead, classify based on the primary industry of the entities and products involved.

## Understanding Article Content

Once you've identified the key entities, you must also understand what the article is actually ABOUT — its subject matter as a news story. Consider:

- **What event or development is being reported?** A news story has a dominant subject: a product launch, a deal, a breach, an earnings result, a policy change, a hiring move, a lawsuit. Identify it.
- **Whose story is this?** The subject of the story is usually the entity the event happens TO or BY — not every entity mentioned. In "SpaceX acquires Cursor for $60B," Cursor is the subject (it's being acquired); SpaceX is the actor. The story is about software M&A, with Cursor as the primary entity.
- **Is the story about the entity's business, or about the entity incidentally?** A data breach at a software company is still a story about that software company. An earnings report from a software company is still a software company story. An office expansion, a hiring announcement, a CEO change, a product policy shift — these are all legitimate news ABOUT a company in its industry.

Do not require the article to literally describe the industry's products or technology to qualify. If the article's dominant subject is a company operating in the specified industry — including business news about that company (funding, hiring, M&A, breaches, policy, earnings, executive moves) — the article belongs to that industry.

## The Two-Factor Test

For an article to belong to the specified industry, BOTH of the following must be true:

1. **Entity Factor**: The main companies, products, or entities in the article belong to the specified industry as their primary business domain
2. **Content Factor**: The article's content is actually about that industry's core concerns, technology, impact, products, services, or developments

If the article is about a company that belongs to the specified industry, but the content is about something unrelated to that industry's core concerns (e.g., a human resources issue, a legal dispute, a financial matter with no industry-specific implications), then the article does NOT belong to the specified industry.

## Step-by-Step Instructions

Follow these steps to complete your classification:

1. **Read and extract**: Read the article and quote multiple relevant passages verbatim that indicate what it's about.

2. **Identify key entities**: List the main companies, products, or subjects discussed. For each, determine its primary industry using the guidance in *Understanding Industry Context* above.

3. **Identify the article's subject**: Determine what the article is actually about — the dominant event or development and the primary entity the story is about — using the guidance in *Understanding Article Content* above.

4. **Apply the Two-Factor Test**:
   - **Entity Factor**: Does the primary subject entity belong to the specified industry as its primary business?
   - **Content Factor**: Is the article's dominant subject a story about an in-industry entity's business (including news like funding, hiring, M&A, breaches, policy, earnings, executive moves)? Or is the in-industry entity only incidentally mentioned in a story whose primary subject is something else?
   - Both factors must be satisfied for a "Yes" classification.

5. **Build and weigh arguments**: List arguments FOR and AGAINST belonging, then weigh them to reach a determination. Remember the article's primary subject (not tangential mentions) must be in-industry.

## Output Format

Provide your response in three sections:

**First**, work through your analysis in <analysis> tags. In this section:
- Quote multiple key passages from the article verbatim that indicate what it's about
- List the main companies, products, or entities mentioned
- For each entity, determine and explicitly state the primary industry/business domain of that entity
- Explicitly state what the article's overall primary industry or domain is
- Apply the Two-Factor Test by answering each factor with "YES" or "NO":
  - **Entity Factor Check**: Answer YES or NO - do the main entities belong to the specified industry as their primary business?
  - **Content Factor Check**: Answer YES or NO - is the article content about the specified industry's core concerns (not tangential matters)?
- Create a structured list of arguments for why the article DOES belong to the specified industry
- Create a structured list of arguments for why the article DOES NOT belong to the specified industry
- Weigh these arguments carefully and reach a determination

Be thorough in this section. It's OK for this section to be quite long, as systematic analysis requires detailed examination.

**Second**, provide your final classification in <classification> tags. Your answer must be exactly one of:
- `Yes` - if the article's major domain belongs to the specified industry
- `No` - if the article's major domain does not belong to the specified industry

**Third**, provide a concise explanation in <reason> tags. Write 1-3 sentences that clearly explain why the article does or does not belong to the specified industry.

### Example Output Structure

<analysis>
Key passages from the article:
- "[verbatim quote 1 from the article]"
- "[verbatim quote 2 from the article]"
- "[verbatim quote 3 from the article]"

Main entities mentioned:
- [Entity Name 1]: Primary industry is [industry name] because [brief explanation of their core business]
- [Entity Name 2]: Primary industry is [industry name] because [brief explanation of their core business]

Article's primary industry/domain: [Clearly state what industry/domain this article is primarily about]

Two-Factor Test:
- Entity Factor: YES/NO - [explain whether main entities belong to specified industry]
- Content Factor: YES/NO - [explain whether content is about specified industry's core concerns vs something tangential]

Arguments FOR the article belonging to [specified industry]:
- [argument 1]
- [argument 2]

Arguments AGAINST the article belonging to [specified industry]:
- [argument 1]
- [argument 2]

Weighing the arguments: [Detailed explanation of which arguments are stronger and why, explicitly considering both the entity factor and content factor, leading to final determination]
</analysis>

<classification>
[Yes or No]
</classification>

<reason>
[1-3 sentence explanation focusing on why the article's primary domain does or does not match the specified industry, referencing both entity and content factors as appropriate]
</reason>

Begin your analysis now.
