from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("decks", "0005_constructed_formats_and_metagame"),
    ]

    operations = [
        migrations.AddField(
            model_name="deck",
            name="coverage",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="deck",
            name="forge_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
